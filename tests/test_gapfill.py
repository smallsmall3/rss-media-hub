"""查漏（gapfill）测试：媒体库缺的集 × 当前 RSS 里现成的资源。

核心是 cross_check —— 纯计算，不需要网络，所以能把匹配规则测得很细。
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from app.config import Subscription
from app.gapfill import GapFinder, GapMatch, GapReport, SeriesGap, item_matches_code, parse_code
from app.libraryscan import (
    STATUS_COMPLETE,
    STATUS_PARTIAL,
    ScanResult,
    SeriesScan,
)
from app.rss import FeedItem


def make_item(title: str, *, season=None, episode=None, size=1_500_000_000, url="", link="") -> FeedItem:
    return FeedItem(
        title=title,
        link=link or url or f"https://pt.example/details?id={title}",
        download_url=url,
        guid=title,
        size_bytes=size,
        published=datetime(2025, 2, 5, 12, 0, tzinfo=timezone.utc),
        season=season,
        episode=episode,
    )


def make_series(
    name: str,
    missing: list[str],
    *,
    owned=3,
    total=6,
    tmdb_id=1,
    status=STATUS_PARTIAL,
    emby_id="s1",
) -> SeriesScan:
    return SeriesScan(
        emby_id=emby_id,
        name=name,
        tmdb_id=tmdb_id,
        total=total,
        aired=total,
        owned=owned,
        status=status,
        missing_codes=list(missing),
    )


class ParseCodeTest(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_code("S01E02"), (1, 2))
        self.assertEqual(parse_code("s4e84"), (4, 84))
        self.assertEqual(parse_code("S100E1000"), (100, 1000))

    def test_invalid(self):
        for bad in ("", "E02", "S01", "hello", "S01E", "全12集", None):
            self.assertEqual(parse_code(bad), (None, None), f"{bad!r} 不该被解析成功")


class ItemMatchesCodeTest(unittest.TestCase):
    def test_exact_season_and_episode(self):
        item = make_item("Show S02E05", season=2, episode=5)
        self.assertTrue(item_matches_code(item, 2, 5))
        self.assertFalse(item_matches_code(item, 2, 6))
        self.assertFalse(item_matches_code(item, 3, 5))

    def test_absolute_numbering_matches_by_episode_only(self):
        """字幕组只写绝对集数（season=None）时应按集号匹配。"""
        item = make_item("[组] 番剧 - 84 [1080p]", season=None, episode=84)
        self.assertTrue(item_matches_code(item, 4, 84))

    def test_item_with_season_but_we_only_care_episode(self):
        item = make_item("Show S04E84", season=4, episode=84)
        # 缺口没写季（season=None）时只看集号
        self.assertTrue(item_matches_code(item, None, 84))

    def test_no_episode_never_matches(self):
        self.assertFalse(item_matches_code(make_item("Show 全12集"), 1, 1))


class CrossCheckTest(unittest.TestCase):
    def setUp(self):
        self.finder = GapFinder.__new__(GapFinder)  # 不需要 settings/http，只测纯计算

    def _scan(self, *series) -> ScanResult:
        result = ScanResult(finished_at=0.0)
        result.series = list(series)
        return result

    def test_matches_available_and_missing(self):
        scan = self._scan(make_series("某剧", ["S01E04", "S01E05", "S01E06"]))
        items = [
            make_item("某剧 S01E05 [1080p]", season=1, episode=5, url="https://pt.example/dl?passkey=K"),
        ]
        report = self.finder.cross_check(scan, items)

        self.assertEqual(report.scanned_series, 1)
        self.assertEqual(report.series_with_gaps, 1)
        self.assertEqual(report.total_missing, 3)
        self.assertEqual(report.total_covered, 1)

        series = report.checks[0]
        self.assertEqual(series.display_name, "某剧")
        avail = series.available_gaps
        self.assertEqual([g.code for g in avail], ["S01E05"])
        best = avail[0].best
        assert best is not None
        self.assertIn("passkey=K", best.download_url)
        self.assertEqual([g.code for g in series.missing_gaps], ["S01E04", "S01E06"])

    def test_multiple_candidates_picks_direct_link_and_biggest(self):
        scan = self._scan(make_series("某剧", ["S01E05"]))
        items = [
            make_item("某剧 S01E05 720p", season=1, episode=5, size=800_000_000),  # 无直链
            make_item("某剧 S01E05 1080p", season=1, episode=5, size=1_500_000_000, url="https://pt/dl/1"),
            make_item("某剧 S01E05 2160p", season=1, episode=5, size=3_000_000_000, url="https://pt/dl/2"),
        ]
        report = self.finder.cross_check(scan, items)
        gap = report.checks[0].gaps[0]
        self.assertEqual(gap.code, "S01E05")
        self.assertEqual(len(gap.items), 3)
        self.assertEqual(gap.best.size_bytes, 3_000_000_000)

    def test_complete_series_ignored(self):
        scan = self._scan(
            make_series("完整的剧", [], status=STATUS_COMPLETE, owned=6, total=6),
            make_series("缺集的剧", ["S01E04"]),
        )
        report = self.finder.cross_check(scan, [])
        self.assertEqual(report.series_with_gaps, 1)
        self.assertEqual(report.checks[0].name, "缺集的剧")

    def test_series_without_gaps_skipped(self):
        scan = self._scan(make_series("怪剧", []))
        report = self.finder.cross_check(scan, [])
        self.assertEqual(report.series_with_gaps, 0)

    def test_unparsable_codes_skipped(self):
        scan = self._scan(make_series("怪剧", ["S01E04", "莫名其妙的集号"]))
        report = self.finder.cross_check(scan, [])
        self.assertEqual([g.code for g in report.checks[0].gaps], ["S01E04"])

    def test_many_gaps_matched_at_once(self):
        scan = self._scan(make_series("某剧", [f"S01E{i:02d}" for i in range(1, 11)]))
        items = [make_item(f"某剧 S01E{i:02d}", season=1, episode=i, url=f"https://pt/dl/{i}") for i in (3, 7, 10)]
        report = self.finder.cross_check(scan, items)
        self.assertEqual(report.total_missing, 10)
        self.assertEqual(report.total_covered, 3)
        self.assertEqual([g.code for g in report.checks[0].available_gaps], ["S01E03", "S01E07", "S01E10"])

    def test_max_series_and_gap_limits(self):
        scan = self._scan(
            make_series("A", ["S01E01", "S01E02"], emby_id="a"),
            make_series("B", ["S01E01"], emby_id="b"),
            make_series("C", ["S01E01"], emby_id="c"),
        )
        report = self.finder.cross_check(scan, [], max_series=2)
        self.assertEqual(report.scanned_series, 3)
        self.assertEqual(len(report.checks), 2)

        report2 = self.finder.cross_check(scan, [], max_gaps_per_series=1)
        self.assertTrue(all(len(s.gaps) == 1 for s in report2.checks))

    def test_absolute_numbering_scenario(self):
        """动漫长季：RSS 用绝对集数（解析出来 season=None），TMDB 用 S04Exx。"""
        scan = self._scan(make_series("进击的巨人", ["S04E85", "S04E86"]))
        items = [
            # 注意 season=None —— 这就是绝对集数命名解析出来的真实结果
            make_item("[组] 进击的巨人 - 85 [1080p]", season=None, episode=85, url="https://pt/dl/85"),
            make_item("[组] 进击的巨人 - 84 [1080p]", season=None, episode=84),
        ]
        report = self.finder.cross_check(scan, items)
        # S04E85 应该匹配到"绝对集数 85"那条
        self.assertEqual([g.code for g in report.checks[0].available_gaps], ["S04E85"])
        self.assertEqual([g.code for g in report.checks[0].missing_gaps], ["S04E86"])

    def test_absolutely_numbered_item_with_guessed_season_still_rejected(self):
        """如果集号被解析成了别的季（S01E85），就不该拿去填 S04E85 —— 这是有意的严格。"""
        scan = self._scan(make_series("某剧", ["S04E85"]))
        items = [make_item("某剧 S01E85", season=1, episode=85)]
        report = self.finder.cross_check(scan, items)
        self.assertEqual(report.total_covered, 0)

    def test_wrong_season_not_matched(self):
        """S02E05 不该被拿去填 S01E05 的缺口。"""
        scan = self._scan(make_series("某剧", ["S01E05"]))
        items = [make_item("某剧 S02E05", season=2, episode=5)]
        report = self.finder.cross_check(scan, items)
        self.assertEqual(report.total_covered, 0)

    def test_empty_inputs(self):
        report = self.finder.cross_check(self._scan(), [])
        self.assertEqual(report.series_with_gaps, 0)
        self.assertEqual(report.total_missing, 0)
        self.assertIn("查漏报告", report.render_text())


class ReportRenderTest(unittest.TestCase):
    def _report(self) -> GapReport:
        report = GapReport(feeds_ok=2, feeds_failed=1, feed_items=37, scanned_series=3)
        report.checks = [
            SeriesGap(
                name="有货剧", display_name="有货剧（2020）", tmdb_id=1, emby_id="a",
                owned=3, total=6,
                gaps=[
                    GapMatch(
                        season=1, episode=4, code="S01E04",
                        items=[make_item("有货剧 S01E04", season=1, episode=4, url="https://pt/dl/4")],
                    )
                ],
            ),
            SeriesGap(
                name="没货剧", display_name="没货剧（2021）", tmdb_id=2, emby_id="b",
                owned=1, total=5,
                gaps=[GapMatch(season=1, episode=2, code="S01E02", items=[])],
            ),
        ]
        report.series_with_gaps = 2
        report.finished_at = report.started_at + 4.0
        report.errors = ["[源A] 连接超时"]
        return report

    def test_text_report(self):
        text = self._report().render_text()
        self.assertIn("查漏报告", text)
        self.assertIn("共缺 2 集", text)
        self.assertIn("其中 1 集当前能在 RSS 里找到", text)
        self.assertIn("有货剧（2020）", text)
        self.assertIn("S01E04", text)
        self.assertIn("暂时没货", text)
        self.assertIn("没货剧（2021）", text)
        # 必须写明能力边界，避免用户误以为能搜全网
        self.assertIn("已翻页的旧集", text)
        self.assertIn("连接超时", text)

    def test_json_report(self):
        data = json.loads(self._report().render_json())
        self.assertEqual(data["feeds_ok"], 2)
        self.assertEqual(data["feed_items"], 37)
        self.assertEqual(data["total_missing"], 2)
        self.assertEqual(data["total_covered"], 1)
        self.assertEqual(len(data["series"]), 2)
        first = data["series"][0]
        self.assertEqual(first["covered"], 1)
        self.assertTrue(first["gaps"][0]["available"])
        self.assertIn("https://pt/dl/4", first["gaps"][0]["best"]["download_url"])
        self.assertFalse(data["series"][1]["gaps"][0]["available"])
        self.assertIsNone(data["series"][1]["gaps"][0]["best"])


class FeedCollectionTest(unittest.IsolatedAsyncioTestCase):
    """collect_feed_items 只抓不重复的地址，并统计成败。"""

    async def test_collects_and_dedupes(self):
        from app import gapfill as gapfill_mod

        settings = type("S", (), {"subscriptions": []})()
        settings.subscriptions = [
            Subscription(id="a", name="源A", mode="feed", rss="https://pt.example/rss1"),
            Subscription(id="b", name="源B", mode="feed", rss="https://pt.example/rss2 https://pt.example/rss1"),
            Subscription(id="c", name="关掉的", mode="feed", rss="https://pt.example/rss3", enabled=False),
            Subscription(id="d", name="追剧", mode="show", tmdb_id=1, rss="https://pt.example/rss4"),
        ]
        finder = GapFinder(settings, http=None)

        calls: list[str] = []

        async def fake_fetch(client, url, **kwargs):
            calls.append(url)
            if url.endswith("rss2"):
                raise RuntimeError("boom")
            return [make_item(f"item-{url}")]

        original = gapfill_mod.fetch_feed
        gapfill_mod.fetch_feed = fake_fetch
        try:
            items, ok, failed, errors = await finder.collect_feed_items()
        finally:
            gapfill_mod.fetch_feed = original

        # rss1（被 a、b 共用）只抓一次；rss3 被禁用跳过；rss4 是 show 源也会抓
        self.assertEqual(calls, ["https://pt.example/rss1", "https://pt.example/rss2", "https://pt.example/rss4"])
        self.assertEqual(ok, 2)
        self.assertEqual(failed, 1)
        self.assertEqual(len(items), 2)
        self.assertTrue(any("boom" in e for e in errors))

    async def test_include_show_feeds_false_skips_them(self):
        from app import gapfill as gapfill_mod

        settings = type("S", (), {"subscriptions": [
            Subscription(id="a", name="全量源", mode="feed", rss="https://pt.example/rss1"),
            Subscription(id="b", name="追剧", mode="show", tmdb_id=1, rss="https://pt.example/rss2"),
        ]})()
        finder = GapFinder(settings, http=None)
        calls: list[str] = []

        async def fake_fetch(client, url, **kwargs):
            calls.append(url)
            return []

        original = gapfill_mod.fetch_feed
        gapfill_mod.fetch_feed = fake_fetch
        try:
            await finder.collect_feed_items(include_show_feeds=False)
        finally:
            gapfill_mod.fetch_feed = original
        self.assertEqual(calls, ["https://pt.example/rss1"])


if __name__ == "__main__":
    unittest.main()
