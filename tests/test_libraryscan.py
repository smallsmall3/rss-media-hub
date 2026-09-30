"""媒体库全库扫描测试（离线）。

覆盖：
  * 四种状态的判定：完整 / 缺集 / 未入库 / 未匹配
  * 缺集区间压缩、百分比
  * JSON 报告结构
  * TMDB 结果持久化缓存（第二次扫描不应再打 TMDB）
  * SeriesInfo <-> JSON 往返不丢数据
"""

from __future__ import annotations

import contextlib
import itertools
import json
import os
import shutil
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import LibrarySettings
from app.db import Database
from app.emby import LocalEpisode
from app.libraryscan import (
    STATUS_COMPLETE,
    STATUS_EMPTY,
    STATUS_PARTIAL,
    STATUS_UNMATCHED,
    LibraryScanner,
    ScanResult,
    SeriesScan,
    _series_from_json,
    _series_to_json,
)
from app.tmdb import EpisodeInfo, SeasonInfo, SeriesInfo, TmdbError, TmdbNotFound

_counter = itertools.count()
TODAY = datetime.now(timezone.utc).date()


@contextlib.contextmanager
def temp_dir():
    base = Path(os.environ.get("RMH_TEST_TMP") or Path(__file__).resolve().parent / ".tmp")
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"scan-{os.getpid()}-{next(_counter)}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def make_series(
    tmdb_id: int,
    name: str,
    seasons: dict[int, list[int]],
    *,
    future_seasons: set[int] | None = None,
) -> SeriesInfo:
    """future_seasons 里的季会被设成"还没播出"。"""
    future_seasons = future_seasons or set()
    past, future = TODAY - timedelta(days=30), TODAY + timedelta(days=30)
    built = [
        SeasonInfo(
            number=number,
            name=f"Season {number}",
            episode_count=len(eps),
            episodes=[
                EpisodeInfo(
                    season=number,
                    episode=e,
                    name=f"E{e}",
                    air_date=future if number in future_seasons else past,
                )
                for e in eps
            ],
        )
        for number, eps in seasons.items()
    ]
    return SeriesInfo(
        tmdb_id=tmdb_id,
        name=name,
        original_name=name,
        year=2020,
        total_seasons=len(built),
        total_episodes=sum(len(v) for v in seasons.values()),
        seasons=built,
    )


class FakeTmdb:
    """假 TMDB：只认识 catalog 里的几部剧。"""

    def __init__(self, catalog: dict[int, SeriesInfo]) -> None:
        self.catalog = catalog
        self.calls = 0

    async def series(self, name, tmdb_id=None, year=None, include_specials=False, **kw):
        self.calls += 1
        if tmdb_id and tmdb_id in self.catalog:
            return self.catalog[tmdb_id]
        for info in self.catalog.values():
            if info.name == name:
                return info
        raise TmdbNotFound(f"TMDB 搜不到剧名「{name}」")


class FakeEmby:
    """假 Emby：list_series 枚举全库，episodes_of 给某部剧的集。"""

    def __init__(self, series: list[dict], episodes: dict[str, set[tuple[int, int]]]) -> None:
        self.series = series
        self.episodes = episodes
        self.list_calls = 0
        self.episode_calls = 0

    async def list_series(self, **kw):
        self.list_calls += 1
        return list(self.series)

    async def episodes_of(self, series_id: str):
        self.episode_calls += 1
        out = []
        for season, episode in sorted(self.episodes.get(series_id, set())):
            out.append(LocalEpisode(season=season, episode=episode, item_id=f"{series_id}-{season}-{episode}"))
        return out


def emby_item(item_id: str, name: str, year=2020, tmdb_id=None) -> dict:
    providers = {"Tmdb": str(tmdb_id)} if tmdb_id else {}
    return {"Id": item_id, "Name": name, "ProductionYear": year, "ProviderIds": providers}


def build_scanner(tmdb: FakeTmdb, emby: FakeEmby, db: Database | None = None, **kw) -> LibraryScanner:
    return LibraryScanner(tmdb, emby, LibrarySettings(url="http://x", api_key="k"), db=db, **kw)


class StatusLogicTest(unittest.IsolatedAsyncioTestCase):
    async def test_complete_partial_empty_unmatched(self):
        tmdb = FakeTmdb(
            {
                1: make_series(1, "Complete Show", {1: [1, 2, 3]}),
                2: make_series(2, "Gap Show", {1: [1, 2, 3, 4]}),
                3: make_series(3, "Empty Show", {1: [1, 2]}),
            }
        )
        emby = FakeEmby(
            series=[
                emby_item("s1", "Complete Show", tmdb_id=1),
                emby_item("s2", "Gap Show", tmdb_id=2),
                emby_item("s3", "Empty Show", tmdb_id=3),
                emby_item("s4", "Unknown Show", tmdb_id=None),
            ],
            episodes={
                "s1": {(1, 1), (1, 2), (1, 3)},
                "s2": {(1, 1), (1, 2), (1, 4)},   # 缺 E3
                "s3": set(),
                "s4": {(1, 1)},
            },
        )
        result = await build_scanner(tmdb, emby).scan()
        by_name = {s.name: s for s in result.series}

        self.assertEqual(by_name["Complete Show"].status, STATUS_COMPLETE)
        self.assertEqual((by_name["Complete Show"].owned, by_name["Complete Show"].total), (3, 3))

        gap = by_name["Gap Show"]
        self.assertEqual(gap.status, STATUS_PARTIAL)
        self.assertEqual((gap.owned, gap.total), (3, 4))
        self.assertEqual(gap.missing_codes, ["S01E03"])
        self.assertEqual(gap.missing_ranges(), "S01E03")
        self.assertAlmostEqual(gap.percent, 75.0)

        self.assertEqual(by_name["Empty Show"].status, STATUS_EMPTY)
        self.assertEqual(by_name["Empty Show"].owned, 0)
        self.assertEqual(by_name["Unknown Show"].status, STATUS_UNMATCHED)

        # 汇总
        self.assertEqual(len(result.partial), 1)
        self.assertEqual(len(result.complete), 1)
        self.assertEqual(len(result.empty), 1)
        self.assertEqual(len(result.unmatched), 1)
        self.assertEqual(result.total_missing_episodes, 1)
        self.assertEqual(result.library_total, 4)
        self.assertEqual(result.scanned, 4)

    async def test_unaired_episodes_not_counted_by_default(self):
        """未播出的集不该拖累进度（默认 count_aired_only=true）。"""
        # 第 1 季已播完（2 集），第 2 季还没播
        tmdb = FakeTmdb({7: make_series(7, "Airing Show", {1: [1, 2], 2: [1]}, future_seasons={2})})
        emby = FakeEmby(
            series=[emby_item("s7", "Airing Show", tmdb_id=7)],
            episodes={"s7": {(1, 1), (1, 2)}},
        )
        scanner = build_scanner(tmdb, emby)
        result = await scanner.scan_one(emby_item("s7", "Airing Show", tmdb_id=7))
        # 第二季那集还没播 → 分母只算第一季 2 集，且应判定为"已完整"
        self.assertEqual(result.total, 2)
        self.assertEqual(result.aired, 2)
        self.assertEqual(result.owned, 2)
        self.assertEqual(result.status, STATUS_COMPLETE)

    async def test_count_aired_only_false_counts_everything(self):
        tmdb = FakeTmdb({7: make_series(7, "Airing Show", {1: [1, 2], 2: [1]}, future_seasons={2})})
        emby = FakeEmby(
            series=[emby_item("s7", "Airing Show", tmdb_id=7)],
            episodes={"s7": {(1, 1), (1, 2)}},
        )
        scanner = build_scanner(tmdb, emby)
        scanner.settings = LibrarySettings(url="http://x", api_key="k", count_aired_only=False)
        result = await scanner.scan_one(emby_item("s7", "Airing Show", tmdb_id=7))
        self.assertEqual(result.total, 3)
        self.assertEqual(result.aired, 2)
        self.assertEqual(result.owned, 2)
        self.assertEqual(result.missing_codes, ["S02E01"])

    async def test_tmdb_error_marks_error_not_crash(self):
        class BoomTmdb:
            async def series(self, *a, **k):
                raise TmdbError("TMDB 限流（429）")

        emby = FakeEmby(series=[emby_item("sx", "X", tmdb_id=99)], episodes={})
        result = await build_scanner(BoomTmdb(), emby).scan_one(emby_item("sx", "X", tmdb_id=99))
        self.assertEqual(result.status, "error")
        self.assertIn("429", result.error)

    async def test_progress_callback(self):
        tmdb = FakeTmdb({i: make_series(i, f"Show {i}", {1: [1]}) for i in range(1, 6)})
        emby = FakeEmby(
            series=[emby_item(f"s{i}", f"Show {i}", tmdb_id=i) for i in range(1, 6)],
            episodes={f"s{i}": {(1, 1)} for i in range(1, 6)},
        )
        seen: list[tuple[int, int]] = []
        await build_scanner(tmdb, emby).scan(on_progress=lambda d, t, s: seen.append((d, t)))
        self.assertEqual(len(seen), 5)
        self.assertEqual(seen[-1][1], 5)
        self.assertEqual(sorted(d for d, _ in seen), [1, 2, 3, 4, 5])

    async def test_limit_truncates(self):
        tmdb = FakeTmdb({i: make_series(i, f"Show {i}", {1: [1]}) for i in range(1, 6)})
        emby = FakeEmby(
            series=[emby_item(f"s{i}", f"Show {i}", tmdb_id=i) for i in range(1, 6)],
            episodes={f"s{i}": {(1, 1)} for i in range(1, 6)},
        )
        result = await build_scanner(tmdb, emby).scan(limit=2)
        self.assertEqual(len(result.series), 2)


class CacheTest(unittest.IsolatedAsyncioTestCase):
    async def test_second_scan_hits_cache(self):
        with temp_dir() as root:
            db = Database(root / "state" / "t.db")
            try:
                tmdb = FakeTmdb({1: make_series(1, "Cached Show", {1: [1, 2]})})
                emby = FakeEmby(
                    series=[emby_item("s1", "Cached Show", tmdb_id=1)],
                    episodes={"s1": {(1, 1), (1, 2)}},
                )
                scanner = build_scanner(tmdb, emby, db=db, cache_ttl=3600)
                await scanner.scan()
                self.assertEqual(tmdb.calls, 1)
                self.assertEqual(await db.count_tmdb_cache(), 1)

                # 新建一个 scanner（模拟容器重启），应命中 SQLite 缓存，不再打 TMDB
                scanner2 = build_scanner(tmdb, emby, db=db, cache_ttl=3600)
                result2 = await scanner2.scan()
                self.assertEqual(tmdb.calls, 1, "第二次扫描不该再请求 TMDB")
                self.assertEqual(result2.series[0].owned, 2)

                # 过期清理
                purged = await db.purge_tmdb_cache(0)
                self.assertEqual(purged, 1)
                self.assertEqual(await db.count_tmdb_cache(), 0)
            finally:
                db.close()

    async def test_expired_cache_is_refetched(self):
        with temp_dir() as root:
            db = Database(root / "state" / "t.db")
            try:
                tmdb = FakeTmdb({1: make_series(1, "S", {1: [1]})})
                emby = FakeEmby(series=[emby_item("s1", "S", tmdb_id=1)], episodes={"s1": {(1, 1)}})
                await build_scanner(tmdb, emby, db=db, cache_ttl=3600).scan()
                self.assertEqual(tmdb.calls, 1)
                # cache_ttl=0 且调用 get_tmdb_cache(max_age=0) 表示"永不过期"；
                # 这里用 max_age=1 模拟过期
                scanner = build_scanner(tmdb, emby, db=db, cache_ttl=3600)
                scanner._mem.clear()
                raw = await db.get_tmdb_cache("tv:1", 1)
                self.assertIsNotNone(raw)
                await db.purge_tmdb_cache(0)
                self.assertIsNone(await db.get_tmdb_cache("tv:1", 1))
            finally:
                db.close()


class SerializationTest(unittest.TestCase):
    def test_series_roundtrip(self):
        info = make_series(42, "往返测试", {1: [1, 2], 2: [1]})
        restored = _series_from_json(json.loads(json.dumps(_series_to_json(info), ensure_ascii=False)))
        self.assertEqual(restored.tmdb_id, 42)
        self.assertEqual(restored.name, "往返测试")
        self.assertEqual([s.number for s in restored.seasons], [1, 2])
        self.assertEqual(len(restored.seasons[0].episodes), 2)
        self.assertEqual(restored.seasons[0].episodes[0].episode, 1)
        self.assertIsNotNone(restored.seasons[0].episodes[0].air_date)

    def test_series_roundtrip_keeps_missing_air_date(self):
        info = make_series(43, "NoAirDate", {1: [1]})
        info.seasons[0].episodes[0].air_date = None
        restored = _series_from_json(json.loads(json.dumps(_series_to_json(info))))
        self.assertIsNone(restored.seasons[0].episodes[0].air_date)


class ReportTest(unittest.TestCase):
    def _sample(self) -> ScanResult:
        result = ScanResult(library_total=10, scanned=10, tmdb_calls=7, cache_hits=3)
        result.series = [
            SeriesScan("a", "A 完整", tmdb_id=1, total=3, aired=3, owned=3, status=STATUS_COMPLETE),
            SeriesScan(
                "b", "B 缺集", tmdb_id=2, total=10, aired=10, owned=7,
                status=STATUS_PARTIAL,
                missing_codes=["S01E03", "S01E04", "S01E05", "S02E01"],
            ),
            SeriesScan("c", "C 未入库", tmdb_id=3, total=5, aired=5, owned=0, status=STATUS_EMPTY),
            SeriesScan("d", "D 未匹配", status=STATUS_UNMATCHED, error="库里没有 TMDB ID"),
        ]
        result.finished_at = result.started_at + 12.5
        return result

    def test_text_report_contains_key_info(self):
        text = self._sample().render_text(top=5)
        self.assertIn("媒体库扫描报告", text)
        self.assertIn("库内剧集 10 部", text)
        self.assertIn("耗时 12.5s", text)
        self.assertIn("TMDB 请求 7 次，缓存命中 3 次", text)
        self.assertIn("共缺 4 集", text)
        # 缺集区间应被压缩
        self.assertIn("S01E03-E05、S02E01", text)
        self.assertIn("7/10", text)
        self.assertIn("未能匹配到 TMDB", text)

    def test_json_report_structure(self):
        data = json.loads(self._sample().render_json())
        self.assertEqual(data["library_total"], 10)
        self.assertEqual(data["summary"]["complete"], 1)
        self.assertEqual(data["summary"]["partial"], 1)
        self.assertEqual(data["summary"]["empty"], 1)
        self.assertEqual(data["summary"]["unmatched"], 1)
        self.assertEqual(data["summary"]["missing_episodes"], 4)
        self.assertEqual(len(data["series"]), 4)
        gap = next(s for s in data["series"] if s["name"] == "B 缺集")
        self.assertEqual(gap["missing"], ["S01E03", "S01E04", "S01E05", "S02E01"])
        self.assertEqual(gap["percent"], 70.0)

    def test_every_series_carries_a_human_status_label(self):
        """界面靠 status_label 显示状态徽标，不能只有内部英文枚举。"""
        from app.libraryscan import STATUS_LABEL

        data = json.loads(self._sample().render_json())
        for series in data["series"]:
            self.assertIn("status_label", series)
            self.assertTrue(series["status_label"], f"{series['name']} 缺少状态文案")
            self.assertEqual(series["status_label"], STATUS_LABEL[series["status"]])
        # 枚举值本身也要保留，方便程序判断
        self.assertEqual(
            {s["status"] for s in data["series"]},
            {STATUS_COMPLETE, STATUS_PARTIAL, STATUS_EMPTY, STATUS_UNMATCHED},
        )

    def test_missing_ranges_compacts(self):
        item = SeriesScan("x", "X", missing_codes=["S01E01", "S01E02", "S01E03", "S01E07", "S02E01", "S02E02"])
        self.assertEqual(item.missing_ranges(), "S01E01-E03、S01E07、S02E01-E02")
        self.assertEqual(item.missing_count, 6)

    def test_missing_ranges_limit(self):
        item = SeriesScan("x", "X", missing_codes=["S01E01", "S01E05", "S01E09", "S01E13"])
        text = item.missing_ranges(limit=2)
        self.assertIn("等 4 集", text)

    def test_percent_zero_total(self):
        self.assertEqual(SeriesScan("x", "X").percent, 0.0)


if __name__ == "__main__":
    unittest.main()
