"""端到端集成测试：RSS → TMDB 比对 → Emby 入库统计 → Telegram 推送 → 追完退订。

四个外部后端（PT 站 RSS / TMDB / Emby / Telegram）全部在进程内替换成可控的假实现，
所以不依赖网络、不需要 httpx 就能验证完整业务闭环：

  1. 首轮只登记、不推送（seed_silent）
  2. 发现新种 → 推送新种通知，且带"入库 x/y 集"
  3. 媒体库新增集数 → 推送入库通知，并把对应 RSS 条目标记完成
  4. 全部入库 → 推送完成通知 + 自动删除订阅（写回 subscriptions.yaml）
  5. 重复轮询不重复推送；未追完不退订

（tests/test_http_backends.py 会在装了 httpx 的环境里再跑一遍真实 HTTP 链路。）
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
from typing import Any

from app import main as main_mod
from app.config import load_settings
from app.emby import LocalEpisode, LocalSeries
from app.main import Hub
from app.rss import FeedItem
from app.tmdb import EpisodeInfo, SeasonInfo, SeriesInfo

_counter = itertools.count()
TODAY = datetime.now(timezone.utc).date()


@contextlib.contextmanager
def temp_dir():
    base = Path(os.environ.get("RMH_TEST_TMP") or Path(__file__).resolve().parent / ".tmp")
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"e2e-{os.getpid()}-{next(_counter)}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def make_series(episodes: dict[int, list[int]], name: str = "Test Show") -> SeriesInfo:
    """构造 TMDB 剧集结构；所有集都设为已播出。"""
    aired = (TODAY - timedelta(days=30)).isoformat()
    seasons = [
        SeasonInfo(
            number=season,
            name=f"Season {season}",
            episode_count=len(nums),
            episodes=[
                EpisodeInfo(season=season, episode=n, name=f"Episode {n}", air_date=TODAY - timedelta(days=30))
                for n in nums
            ],
        )
        for season, nums in episodes.items()
    ]
    return SeriesInfo(
        tmdb_id=777,
        name=name,
        original_name=name,
        year=2020,
        status="Returning Series",
        total_seasons=len(seasons),
        total_episodes=sum(len(v) for v in episodes.values()),
        seasons=seasons,
    )


def make_local(seasons: dict[int, list[int]]) -> LocalSeries:
    series = LocalSeries(item_id="series-1", name="Test Show", year=2020, provider_ids={"Tmdb": "777"})
    for season, nums in seasons.items():
        for n in nums:
            series.episodes.append(
                LocalEpisode(
                    season=season,
                    episode=n,
                    name=f"Episode {n}",
                    item_id=f"ep-{season}-{n}",
                    path=f"/media/Test Show/Season {season:02d}/Test Show S{season:02d}E{n:02d}.mkv",
                )
            )
    return series


class FakeBackends:
    """进程内假后端。"""

    def __init__(self) -> None:
        self.rss_episodes: list[int] = []      # RSS 里有哪几集
        self.library: dict[int, list[int]] = {}  # 媒体库里已入库的 (季 -> 集号列表)
        self.tmdb_episodes: list[int] = [1, 2, 3, 4]  # TMDB 上这一季有多少集
        self.telegram: list[str] = []
        self.rss_calls = 0
        self.rss_title_template = "Test Show S01E{n:02d} [1080p][WEB-DL]"
        self.rss_is_atom = False
        # 全库扫描用：Emby 里的剧集列表 + 每部剧的集 + TMDB 目录
        self.library_series: list[dict[str, Any]] = []
        self.library_episodes: dict[str, dict[int, list[int]]] = {}
        self.tmdb_catalog: dict[int, list[int]] = {777: [1, 2, 3, 4]}

    # ---------------- 假实现 ----------------
    async def fetch_feed(self, client: Any, url: str, **kwargs: Any) -> list[FeedItem]:
        self.rss_calls += 1
        items: list[FeedItem] = []
        for idx, ep in enumerate(self.rss_episodes):
            published = datetime.now(timezone.utc) - timedelta(hours=len(self.rss_episodes) - idx)
            if self.rss_is_atom:
                # 动漫常见写法：只有绝对集号，没有 SxxExx
                items.append(
                    FeedItem(
                        title=f"[组] Test Show - {ep:02d} [1080p]",
                        link=f"https://mikan.example/Episode/{ep}",
                        download_url=f"magnet:?xt=urn:btih:FAKE{ep}",
                        guid=f"mikan-{ep}",
                        published=published,
                        episode=ep,
                    )
                )
                continue
            title = self.rss_title_template.format(n=ep)
            items.append(
                FeedItem(
                    title=title,
                    link=f"https://pt.example/details.php?id={ep}",
                    download_url=f"https://pt.example/download.php?id={ep}&passkey=SECRET",
                    guid=f"pt-{ep}",
                    published=published,
                    size_bytes=1_500_000_000,
                    season=1,
                    episode=ep,
                )
            )
        return items

    async def tmdb_series(self, name: str | None, tmdb_id: int | None = None, year: int | None = None, **kw: Any) -> SeriesInfo:
        # 全库扫描路径按 tmdb_id 查：登记的 id 用 catalog，没登记的退回 tmdb_episodes
        # （这样老测试里 tmdb_id=777 的语义保持不变）
        if tmdb_id is not None:
            episodes = self.tmdb_catalog.get(tmdb_id)
            if tmdb_id in self.tmdb_catalog and episodes is None:
                from app.tmdb import TmdbNotFound

                raise TmdbNotFound(f"TMDB 搜不到剧名「{name}」")
            info = make_series({1: list(episodes if episodes is not None else self.tmdb_episodes)})
            info.tmdb_id = tmdb_id
            if name:
                info.name = name
            return info
        return make_series({1: list(self.tmdb_episodes)})

    async def emby_find(self, name: str, **kw: Any) -> LocalSeries | None:
        if not self.library:
            return None
        return make_local(self.library)

    async def detect_kind(self) -> str:
        return "emby"

    async def resolve_user_id(self) -> str:
        return "user-1"

    async def ping(self) -> dict[str, Any]:
        return {"ProductName": "Emby", "Version": "4.8.0.0"}

    async def poster_bytes(self, series: LocalSeries) -> bytes | None:
        return None

    # ---- 全库扫描用的假实现 ----
    async def emby_list_series(self, **kw: Any) -> list[dict[str, Any]]:
        return list(self.library_series)

    async def emby_episodes_of(self, series_id: str) -> list[LocalEpisode]:
        out: list[LocalEpisode] = []
        for season, eps in sorted(self.library_episodes.get(series_id, {}).items()):
            for e in sorted(eps):
                out.append(LocalEpisode(season=season, episode=e, item_id=f"{series_id}-{season}-{e}"))
        return out

    # ---------------- 打桩 ----------------
    def install(self, hub: Hub) -> None:
        main_mod.fetch_feed = self.fetch_feed  # type: ignore[assignment]

        async def fake_series(*args: Any, **kwargs: Any) -> SeriesInfo:
            return await self.tmdb_series(*args, **kwargs)

        async def fake_find(name: str, **kwargs: Any) -> LocalSeries | None:
            return await self.emby_find(name, **kwargs)

        async def fake_list_series(**kwargs: Any) -> list[dict[str, Any]]:
            return await self.emby_list_series(**kwargs)

        async def fake_episodes_of(series_id: str) -> list[LocalEpisode]:
            return await self.emby_episodes_of(series_id)

        async def fake_send_message(text: str, **kwargs: Any) -> Any:
            self.telegram.append(text)
            from app.telegram import TgResult

            return TgResult(True, message_id=len(self.telegram))

        async def fake_get_me() -> dict[str, Any]:
            return {"username": "fake_bot"}

        hub.tmdb.series = fake_series  # type: ignore[assignment]
        hub.library.find_series = fake_find  # type: ignore[assignment]
        hub.library.list_series = fake_list_series  # type: ignore[assignment]
        hub.library.episodes_of = fake_episodes_of  # type: ignore[assignment]
        hub.library.detect_kind = self.detect_kind  # type: ignore[assignment]
        hub.library.resolve_user_id = self.resolve_user_id  # type: ignore[assignment]
        hub.library.ping = self.ping  # type: ignore[assignment]
        hub.library.poster_bytes = self.poster_bytes  # type: ignore[assignment]
        hub.tg.send_message = fake_send_message  # type: ignore[assignment]
        hub.tg.get_me = fake_get_me  # type: ignore[assignment]


class EndToEndTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.backends = FakeBackends()
        os.environ.update(
            {
                "RMH_TG_BOT_TOKEN": "111:FAKE",
                "RMH_TG_CHAT_ID": "-100123",
                "RMH_TG_SEND_POSTER": "false",
                "RMH_TMDB_API_KEY": "fake-key",
                "RMH_TMDB_LANGUAGE": "zh-CN",
                "RMH_EMBY_URL": "http://emby.example:8096",
                "RMH_EMBY_API_KEY": "fake-emby-key",
                "RMH_EMBY_KIND": "emby",
                "RMH_EMBY_CACHE_TTL": "0",
                "RMH_POLL_INTERVAL": "900",
                "RMH_RECONCILE_INTERVAL": "1800",
                "RMH_SEED_SILENT": "true",
                "RMH_HEALTH_PORT": "0",
                "RMH_LOG_LEVEL": "CRITICAL",
            }
        )
        self._hubs: list[Hub] = []
        self._orig_fetch = main_mod.fetch_feed

    async def asyncTearDown(self) -> None:
        main_mod.fetch_feed = self._orig_fetch
        for key in list(os.environ):
            if key.startswith("RMH_"):
                os.environ.pop(key, None)
        for hub in self._hubs:
            await hub.aclose()

    # ---------------------------------------------------------------
    async def _build_hub(self, root: Path, subs_yaml: str | None = None) -> Hub:
        config_dir = root / "config"
        (config_dir).mkdir(parents=True, exist_ok=True)
        (config_dir / "subscriptions.yaml").write_text(
            subs_yaml
            or (
                "subscriptions:\n"
                "  - id: test-show\n"
                "    name: Test Show\n"
                "    tmdb_id: 777\n"
                "    rss: https://pt.example/rss?passkey=SECRET\n"
                "    remove_when_done: true\n"
            ),
            encoding="utf-8",
        )
        hub = Hub(load_settings(config_dir, root / "state"))
        self.backends.install(hub)
        self._hubs.append(hub)
        return hub

    async def _poll(self, hub: Hub) -> None:
        for rt in list(hub.runtimes.values()):
            await hub.poll_subscription(rt)

    def _sub(self, hub: Hub):
        return next(iter(hub.runtimes.values())).sub

    @contextlib.contextmanager
    def _patch_hub(self, hub: Hub):
        """让 CLI 命令（它们内部会自己 Hub(settings)）复用我们打好桩的 hub 实例。"""
        original = main_mod.Hub
        main_mod.Hub = lambda settings: hub  # type: ignore[assignment]
        try:
            yield hub
        finally:
            main_mod.Hub = original  # type: ignore[assignment]

    # ---------------------------------------------------------------
    async def test_full_lifecycle(self):
        with temp_dir() as root:
            hub = await self._build_hub(root)
            sub = self._sub(hub)

            # ---------- 1) 首轮：RSS 里只有 E1/E2，静默登记 ----------
            self.backends.rss_episodes = [1, 2]
            self.backends.library = {}
            await self._poll(hub)
            self.assertEqual(self.backends.telegram, [], "首轮不应该推送任何消息")
            state = await hub.db.get_sub(sub.id)
            self.assertEqual((state.owned, state.total), (0, 0), "首轮静默登记时不做比对")
            self.assertIn("已登记 2 条", state.extra)
            self.assertEqual(await hub.db.count_items(sub.id), (2, 2), "两条历史条目应登记为已处理")

            # ---------- 2) 出现新种 E3 → 推送新种通知（带入库进度） ----------
            self.backends.rss_episodes = [1, 2, 3]
            await self._poll(hub)
            self.assertEqual(len(self.backends.telegram), 1, self.backends.telegram)
            msg = self.backends.telegram[-1]
            self.assertIn("Test Show", msg)
            self.assertIn("S01E03", msg)
            self.assertIn("0/4", msg)
            self.assertIn("passkey=SECRET", msg, "下载链接必须带 passkey")
            self.assertIn("1.40 GB", msg)
            state = await hub.db.get_sub(sub.id)
            self.assertEqual((state.owned, state.total, state.aired), (0, 4, 4))

            # ---------- 3) 重复轮询不重复推送 ----------
            await self._poll(hub)
            self.assertEqual(len(self.backends.telegram), 1, "重复轮询不应重发")

            # ---------- 4) 媒体库入库 E1-E3 → 推送入库通知 ----------
            self.backends.library = {1: [1, 2, 3]}
            await hub.reconcile_subscription(sub, notify=True)
            self.assertEqual(len(self.backends.telegram), 2, self.backends.telegram)
            update_msg = self.backends.telegram[-1]
            self.assertIn("3/4", update_msg)
            self.assertIn("S01E03", update_msg)
            self.assertTrue((root / "config" / "subscriptions.yaml").exists(), "未追完不应退订")

            # 已入库的集不应再作为"新种"推送
            await self._poll(hub)
            self.assertEqual(len(self.backends.telegram), 2)

            # ---------- 5) 最后一集入库 → 先推入库通知，再推完成通知 + 退订 ----------
            self.backends.library = {1: [1, 2, 3, 4]}
            await hub.reconcile_subscription(sub, notify=True)
            self.assertEqual(len(self.backends.telegram), 4, self.backends.telegram)
            self.assertIn("4/4", self.backends.telegram[-2])
            done_msg = self.backends.telegram[-1]
            self.assertIn("订阅完成", done_msg)
            self.assertIn("4/4", done_msg)
            self.assertIn("已自动删除", done_msg)

            remaining = (root / "config" / "subscriptions.yaml").read_text(encoding="utf-8")
            self.assertNotIn("test-show", remaining, "追完后订阅应从 YAML 中删除")
            self.assertEqual(hub.runtimes, {})
            self.assertEqual(hub.stats.removals, 1)

    async def test_not_done_when_episode_missing(self):
        with temp_dir() as root:
            hub = await self._build_hub(root)
            sub = self._sub(hub)
            self.backends.rss_episodes = [1, 2]
            self.backends.library = {1: [1, 2, 4]}  # 缺 S01E03
            await self._poll(hub)
            result = await hub.reconcile_subscription(sub, notify=True)
            self.assertFalse(result.done)
            self.assertEqual((result.owned, result.total, result.aired), (3, 4, 4))
            self.assertEqual([m.code for m in result.missing], ["S01E03"])
            self.assertTrue((root / "config" / "subscriptions.yaml").exists(), "未完成不应退订")

    async def test_remove_when_done_false_keeps_subscription(self):
        with temp_dir() as root:
            hub = await self._build_hub(
                root,
                subs_yaml=(
                    "subscriptions:\n"
                    "  - id: keep-me\n"
                    "    name: Test Show\n"
                    "    tmdb_id: 777\n"
                    "    rss: https://pt.example/rss\n"
                    "    remove_when_done: false\n"
                ),
            )
            sub = self._sub(hub)
            self.backends.rss_episodes = [1]
            self.backends.library = {1: [1, 2, 3, 4]}
            await self._poll(hub)          # 首轮静默登记 + 存基线
            await hub.reconcile_subscription(sub, notify=True)
            text = (root / "config" / "subscriptions.yaml").read_text(encoding="utf-8")
            self.assertIn("keep-me", text, "remove_when_done=false 时不应删除订阅")
            self.assertTrue(any("订阅完成" in m for m in self.backends.telegram))

    async def test_atom_and_magnet_fallback(self):
        """Atom + magnet 的动漫场景：兜底解析同样应触发入库通知。"""
        with temp_dir() as root:
            hub = await self._build_hub(root)
            sub = self._sub(hub)
            self.backends.rss_is_atom = True
            self.backends.tmdb_catalog = {777: [1, 2, 3, 4, 5]}  # 动漫长季
            self.backends.rss_episodes = [5]
            self.backends.library = {}
            await self._poll(hub)  # 首轮静默登记
            self.assertEqual(self.backends.telegram, [])
            self.backends.library = {1: [1, 2, 3, 4, 5]}
            await hub.reconcile_subscription(sub, notify=True)
            joined = "\n".join(self.backends.telegram)
            self.assertIn("S01E05", joined)
            self.assertIn("Test Show", joined)

    async def test_filters_block_and_allow(self):
        """quality / exclude_filter 规则真的会挡掉条目，而且挡掉的条目不会补推。"""
        with temp_dir() as root:
            hub = await self._build_hub(
                root,
                subs_yaml=(
                    "subscriptions:\n"
                    "  - id: filtered\n"
                    "    name: Test Show\n"
                    "    tmdb_id: 777\n"
                    "    rss: https://pt.example/rss\n"
                    "    quality: ['2160p']\n"
                    "    exclude_filter: '预告'\n"
                ),
            )
            sub = self._sub(hub)
            self.backends.rss_episodes = [1]
            await self._poll(hub)  # 首轮静默登记
            await self._poll(hub)
            self.assertEqual(self.backends.telegram, [], "1080p 应被 quality=2160p 挡掉")

            # 换成 2160p 的新集 → 应该推送
            self.backends.rss_title_template = "Test Show S01E{n:02d} [2160p][WEB-DL]"
            self.backends.rss_episodes = [1, 2]
            await self._poll(hub)
            self.assertEqual(len(self.backends.telegram), 1, self.backends.telegram)
            self.assertIn("S01E02", self.backends.telegram[-1])

            # 预告即使符合画质也要被 exclude_filter 挡掉
            self.backends.rss_title_template = "Test Show S01E{n:02d} [2160p] 预告"
            self.backends.rss_episodes = [1, 2, 3]
            await self._poll(hub)
            self.assertEqual(len(self.backends.telegram), 1, "预告不应推送")

    async def test_reconcile_error_is_reported_not_crashing(self):
        """TMDB 挂了要记录错误状态，而不是把整个循环搞崩。"""
        with temp_dir() as root:
            hub = await self._build_hub(root)
            sub = self._sub(hub)
            from app.tmdb import TmdbError

            async def boom(*args: Any, **kwargs: Any) -> SeriesInfo:
                raise TmdbError("TMDB 限流（429）")

            hub.tmdb.series = boom  # type: ignore[assignment]
            result = await hub.reconcile_subscription(sub, notify=True)
            self.assertFalse(result.ok)
            self.assertIn("429", result.error)
            state = await hub.db.get_sub(sub.id)
            self.assertEqual(state.state, "error")
            self.assertIn("429", state.last_error)
            self.assertEqual(self.backends.telegram, [], "出错时不应推送")

    async def test_scan_command_prints_report_and_exports_json(self):
        """`python -m app scan` 走**真实扫描流程**（只把 Emby/TMDB 换成假后端）。"""
        import io
        from contextlib import redirect_stdout

        from app.main import cmd_scan

        with temp_dir() as root:
            hub = await self._build_hub(root)

            # 库里有两部剧：一部缺 S01E04，一部完整
            self.backends.library_series = [
                {"Id": "series-1", "Name": "Test Show", "ProductionYear": 2020, "ProviderIds": {"Tmdb": "777"}},
                {"Id": "series-2", "Name": "Done Show", "ProductionYear": 2021, "ProviderIds": {"Tmdb": "888"}},
            ]
            self.backends.library_episodes = {
                "series-1": {1: [1, 2, 3]},   # TMDB 有 4 集 → 缺 E04
                "series-2": {1: [1, 2]},
            }
            self.backends.tmdb_catalog = {777: [1, 2, 3, 4], 888: [1, 2]}

            export = root / "report.json"
            buf = io.StringIO()
            with self._patch_hub(hub), redirect_stdout(buf):
                code = await cmd_scan(hub.settings, export=str(export))

            output = buf.getvalue()
            self.assertEqual(code, 0, output)
            self.assertIn("媒体库扫描报告", output)
            self.assertIn("Test Show", output)
            self.assertIn("S01E04", output)
            self.assertIn("共缺 1 集", output)
            self.assertTrue(export.exists(), "没有导出 JSON 报告")

            data = json.loads(export.read_text(encoding="utf-8"))
            self.assertEqual(data["library_total"], 2)
            self.assertEqual(data["scanned"], 2)
            self.assertEqual(data["summary"]["partial"], 1)
            self.assertEqual(data["summary"]["complete"], 1)
            self.assertEqual(data["summary"]["missing_episodes"], 1)
            gap = next(s for s in data["series"] if s["name"] == "Test Show")
            self.assertEqual(gap["missing"], ["S01E04"])
            self.assertEqual((gap["owned"], gap["total"]), (3, 4))

            # --json 模式：stdout 必须是纯 JSON（提示信息走 stderr），这样才能直接喂给 jq
            hub2 = await self._build_hub(root)
            buf = io.StringIO()
            err = io.StringIO()
            with self._patch_hub(hub2), redirect_stdout(buf), contextlib.redirect_stderr(err):
                code = await cmd_scan(hub2.settings, json_only=True)
            self.assertEqual(code, 0)
            payload = json.loads(buf.getvalue().strip())  # 整段都应是合法 JSON
            self.assertEqual(payload["scanned"], 2)
            self.assertIn("开始扫描", err.getvalue(), "提示信息应该出现在 stderr")

    async def test_scan_uses_cache_on_second_run(self):
        """第二次扫描应命中 SQLite 里的 TMDB 缓存，不再打 TMDB。"""
        import io
        from contextlib import redirect_stdout

        from app.main import cmd_scan

        with temp_dir() as root:
            hub = await self._build_hub(root)
            self.backends.library_series = [
                {"Id": "series-1", "Name": "Test Show", "ProductionYear": 2020, "ProviderIds": {"Tmdb": "777"}},
            ]
            self.backends.library_episodes = {"series-1": {1: [1, 2, 3, 4]}}
            self.backends.tmdb_catalog = {777: [1, 2, 3, 4]}

            calls = {"n": 0}
            real_series = self.backends.tmdb_series

            async def counting_series(*args: Any, **kwargs: Any) -> SeriesInfo:
                calls["n"] += 1
                return await real_series(*args, **kwargs)

            hub.tmdb.series = counting_series  # type: ignore[assignment]

            with self._patch_hub(hub), redirect_stdout(io.StringIO()):
                await cmd_scan(hub.settings)
            self.assertEqual(calls["n"], 1, "第一次应该真的查 TMDB")

            # cmd_scan 内部会关闭它用的那个 hub（连带关闭 SQLite），
            # 所以这里重新打开同一个数据库文件来检查缓存
            from app.db import Database

            probe = Database(hub.settings.db_file)
            try:
                self.assertEqual(await probe.count_tmdb_cache(), 1, "应该写入了 TMDB 缓存")
            finally:
                probe.close()

            # 第二次：新的 Hub 实例（模拟容器重启），应该走 SQLite 缓存
            hub2 = await self._build_hub(root)
            hub2.tmdb.series = counting_series  # type: ignore[assignment]
            with self._patch_hub(hub2), redirect_stdout(io.StringIO()):
                await cmd_scan(hub2.settings)
            self.assertEqual(calls["n"], 1, "第二次不该再查 TMDB（应命中缓存）")

    async def test_scan_command_requires_library_and_tmdb(self):
        """没配 Emby / TMDB 时要给出明确提示，而不是崩掉。"""
        import io
        from contextlib import redirect_stdout

        from app.main import cmd_scan

        with temp_dir() as root:
            hub = await self._build_hub(root)
            hub.settings.library.api_key = ""
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = await cmd_scan(hub.settings)
            self.assertEqual(code, 1)
            self.assertIn("Emby", buf.getvalue())

            hub.settings.library.api_key = "k"
            hub.settings.tmdb.api_key = ""
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = await cmd_scan(hub.settings)
            self.assertEqual(code, 1)
            self.assertIn("TMDB", buf.getvalue())

    async def test_feed_mode_pushes_everything_without_library(self):
        """feed 模式（你描述的场景）：给个 RSS，来新数据就推给 TG，不比对媒体库。

        关键点：
          * 不需要 Emby/TMDB 也能工作
          * 首轮就把现有条目推过来（seed=false），不做静默登记
          * 之后每轮只推新增的，不重复
          * 完全不会去调用巡检（不会打 TMDB/Emby）
        """
        with temp_dir() as root:
            hub = await self._build_hub(
                root,
                subs_yaml=(
                    "subscriptions:\n"
                    "  - id: my-pt\n"
                    "    name: 我的 PT 源\n"
                    "    mode: feed\n"
                    "    rss: https://pt.example/rss?passkey=SECRET\n"
                    "    seed: false\n"
                ),
            )
            sub = self._sub(hub)
            self.assertTrue(sub.is_feed)

            # 记录有没有人去动媒体库/TMDB
            recon_calls = {"n": 0}
            original_reconcile = hub.reconciler.reconcile

            async def counting_reconcile(*args: Any, **kwargs: Any):
                recon_calls["n"] += 1
                return await original_reconcile(*args, **kwargs)

            hub.reconciler.reconcile = counting_reconcile  # type: ignore[assignment]

            # ---------- 第一轮：RSS 里已有 2 条，应该直接推过来 ----------
            self.backends.rss_episodes = [1, 2]
            await self._poll(hub)
            self.assertEqual(len(self.backends.telegram), 1, self.backends.telegram)
            msg = self.backends.telegram[0]
            self.assertIn("我的 PT 源", msg)
            self.assertIn("S01E01", msg)
            self.assertIn("S01E02", msg)
            self.assertIn("passkey=SECRET", msg)
            # feed 模式不该出现入库进度
            self.assertNotIn("入库", msg)
            self.assertNotIn("集数统计", msg)
            self.assertEqual(recon_calls["n"], 0, "feed 模式不应该去比对媒体库")

            # ---------- 第二轮：没有新条目，不该重复推 ----------
            await self._poll(hub)
            self.assertEqual(len(self.backends.telegram), 1, "重复轮询不应重发")

            # ---------- 第三轮：来了新的一条 E3，只推它 ----------
            self.backends.rss_episodes = [1, 2, 3]
            await self._poll(hub)
            self.assertEqual(len(self.backends.telegram), 2, self.backends.telegram)
            latest = self.backends.telegram[-1]
            self.assertIn("S01E03", latest)
            self.assertNotIn("S01E01", latest, "只应推送新增的那一条")

            # ---------- 巡检循环也要跳过 feed 订阅 ----------
            result = await hub.reconcile_subscription(sub, notify=True)
            self.assertFalse(result.ok)
            self.assertIn("feed", result.error)
            self.assertEqual(recon_calls["n"], 0, "feed 模式永远不该比对媒体库")

            # ---------- 记录统计 ----------
            counts = await hub.db.count_items(sub.id)
            self.assertEqual(counts[0], 3, "3 条都应登记")
            self.assertEqual(counts[1], 3, "3 条都应标记为已推送")

    async def test_feed_mode_works_without_tmdb_and_emby(self):
        """feed 模式的卖点之一：没配 Emby/TMDB 也能用。"""
        with temp_dir() as root:
            hub = await self._build_hub(
                root,
                subs_yaml=(
                    "subscriptions:\n"
                    "  - id: raw\n"
                    "    name: 纯转发\n"
                    "    mode: feed\n"
                    "    rss: https://pt.example/rss\n"
                    "    seed: false\n"
                ),
            )
            # 把 Emby / TMDB 配置清空
            hub.settings.library.api_key = ""
            hub.settings.tmdb.api_key = ""
            self.assertFalse(hub.settings.library.enabled)
            self.assertFalse(hub.settings.tmdb.enabled)

            self.backends.rss_episodes = [7]
            await self._poll(hub)
            self.assertEqual(len(self.backends.telegram), 1, self.backends.telegram)
            self.assertIn("S01E07", self.backends.telegram[0])

    async def test_feed_mode_seed_true_keeps_history_silent(self):
        """如果用户不想被历史条目刷屏，seed=true 时首轮只登记不推送。"""
        with temp_dir() as root:
            hub = await self._build_hub(
                root,
                subs_yaml=(
                    "subscriptions:\n"
                    "  - id: quiet\n"
                    "    name: 安静模式\n"
                    "    mode: feed\n"
                    "    rss: https://pt.example/rss\n"
                    "    seed: true\n"
                ),
            )
            self.backends.rss_episodes = [1, 2, 3]
            await self._poll(hub)
            self.assertEqual(self.backends.telegram, [], "首轮应该静默")

            self.backends.rss_episodes = [1, 2, 3, 4]
            await self._poll(hub)
            self.assertEqual(len(self.backends.telegram), 1)
            self.assertIn("S01E04", self.backends.telegram[0])
            self.assertNotIn("S01E01", self.backends.telegram[0])

    async def test_feed_mode_filters_apply(self):
        """feed 模式同样支持正则/画质过滤——这是"全量"但不等于"什么都推"。"""
        with temp_dir() as root:
            hub = await self._build_hub(
                root,
                subs_yaml=(
                    "subscriptions:\n"
                    "  - id: filtered-feed\n"
                    "    name: 过滤后的源\n"
                    "    mode: feed\n"
                    "    rss: https://pt.example/rss\n"
                    "    seed: false\n"
                    "    quality: ['2160p']\n"
                    "    exclude_filter: '预告'\n"
                ),
            )
            # 默认模板是 1080p，会被画质白名单挡掉
            self.backends.rss_episodes = [1]
            await self._poll(hub)
            self.assertEqual(self.backends.telegram, [], "1080p 应被 2160p 白名单挡掉")

            # 换成 2160p 就该推
            self.backends.rss_title_template = "Test Show S01E{n:02d} [2160p]"
            self.backends.rss_episodes = [1, 2]
            await self._poll(hub)
            self.assertEqual(len(self.backends.telegram), 1, self.backends.telegram)
            self.assertIn("S01E02", self.backends.telegram[0])

            # 预告即使符合画质也要被挡
            self.backends.rss_title_template = "Test Show S01E{n:02d} [2160p] 预告"
            self.backends.rss_episodes = [1, 2, 3]
            await self._poll(hub)
            self.assertEqual(len(self.backends.telegram), 1, "预告不该推送")

    async def test_show_mode_still_works_after_feed_changes(self):
        """加了 feed 模式后，原来的 show（按剧追踪）行为不能被破坏。"""
        with temp_dir() as root:
            hub = await self._build_hub(root)  # 默认夹具是 show 模式
            sub = self._sub(hub)
            self.assertTrue(sub.is_show)

            self.backends.rss_episodes = [1, 2]
            self.backends.library = {}
            await self._poll(hub)  # 首轮静默
            self.assertEqual(self.backends.telegram, [])

            self.backends.rss_episodes = [1, 2, 3]
            await self._poll(hub)
            self.assertEqual(len(self.backends.telegram), 1)
            msg = self.backends.telegram[-1]
            # show 模式必须带入库进度
            self.assertIn("0/4", msg)
            self.assertIn("待入库", msg)

    async def test_poll_cadence_honours_2_to_5_minute_interval(self):
        """用户要的是"每 2-5 分钟自动刷新"，这里验证真实调度代码按这个节奏走。"""
        import time as _time

        for interval in (120, 180, 300):
            with temp_dir() as root:
                os.environ["RMH_POLL_INTERVAL"] = str(interval)
                try:
                    hub = await self._build_hub(root)
                finally:
                    os.environ.pop("RMH_POLL_INTERVAL", None)

                # 1) 配置确实生效
                self.assertEqual(hub.settings.poll_interval, interval)
                self.assertEqual(hub.poll_interval_seconds(), interval)

                # 2) 首次排期把轮询错峰摊开，落在 [now, now+interval]
                rt = next(iter(hub.runtimes.values()))
                now = _time.time()
                self.assertGreater(rt.next_poll, now - 1)
                self.assertLessEqual(rt.next_poll, now + interval + 1)

                # 3) 轮询完成后重新排期：至少一个周期，最多 1.1 个周期（含抖动）
                delay = hub._next_poll_delay(interval)
                self.assertGreaterEqual(delay, interval)
                self.assertLessEqual(delay, interval * 1.1 + 0.001)

        # 4) 下限保护：填太小的值不会被真的按 10 秒去打站
        with temp_dir() as root:
            os.environ["RMH_POLL_INTERVAL"] = "5"
            try:
                hub = await self._build_hub(root)
            finally:
                os.environ.pop("RMH_POLL_INTERVAL", None)
            self.assertEqual(hub.poll_interval_seconds(), 60, "再快也不能快过 60 秒")

    async def test_feed_mode_dashboard_payload(self):
        """网页界面要能区分两种模式（feed 不显示进度条）。"""
        with temp_dir() as root:
            hub = await self._build_hub(
                root,
                subs_yaml=(
                    "subscriptions:\n"
                    "  - id: f1\n"
                    "    name: 全量源\n"
                    "    mode: feed\n"
                    "    rss: https://pt.example/rss\n"
                    "  - id: s1\n"
                    "    name: 追剧\n"
                    "    mode: show\n"
                    "    tmdb_id: 777\n"
                    "    rss: https://pt.example/rss2\n"
                ),
            )
            from app.webserve import WebUI

            ui = WebUI(hub, host="127.0.0.1", port=0)
            try:
                payload = await ui.h_dashboard({}, {}, b"")
            finally:
                await ui.stop()
            by_id = {s["id"]: s for s in payload["subscriptions"]}
            self.assertEqual(by_id["f1"]["mode"], "feed")
            self.assertEqual(by_id["f1"]["mode_label"], "订阅源全量")
            self.assertEqual(by_id["s1"]["mode"], "show")
            self.assertEqual(by_id["s1"]["mode_label"], "按剧追踪")

    async def test_http_proxy_is_actually_used(self):
        """配了正向代理后，请求必须真的发给代理服务器。

        用一个假代理服务器验证两种形态：
          * HTTPS 目标（TMDB 默认）→ 代理会先收到 CONNECT host:443，即"建隧道"
          * HTTP 目标 → 代理会收到 GET http://... 这种绝对 URL 的请求行
        两者都能证明请求确实绕道代理了。
        """
        import asyncio

        seen: list[str] = []

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                head = await asyncio.wait_for(reader.read(4096), timeout=5)
                line = head.decode("utf-8", "ignore").split("\r\n")[0]
                seen.append(line)
                if line.startswith("GET "):
                    body = b'{"id":777,"name":"Proxied","seasons":[]}'
                    writer.write(
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                        + str(len(body)).encode()
                        + b"\r\nConnection: close\r\n\r\n"
                        + body
                    )
                else:
                    # CONNECT：假装隧道建立失败，让客户端立刻报错退出（我们只关心请求到了代理）
                    writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                with contextlib.suppress(Exception):
                    await writer.drain()
            except Exception:  # noqa: BLE001
                pass
            with contextlib.suppress(Exception):
                writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        proxy = f"http://127.0.0.1:{port}"
        try:
            from app.tmdb import TmdbClient, TmdbError

            # --- 情况 1：HTTPS 目标（默认 api_base）→ 代理应收到 CONNECT ---
            client = TmdbClient("fake-key", proxy=proxy, timeout=5.0)
            try:
                with self.assertRaises(TmdbError):
                    await client._get("/tv/777", retries=1)
            finally:
                await client.aclose()
            self.assertTrue(seen, "代理服务器完全没收到请求，说明代理没生效")
            self.assertTrue(
                seen[-1].startswith("CONNECT "),
                f"HTTPS 目标应该走 CONNECT 隧道，实际收到：{seen[-1]}",
            )
            self.assertIn("api.themoviedb.org:443", seen[-1])

            # --- 情况 2：HTTP 目标 → 代理应收到绝对 URL 的 GET ---
            seen.clear()
            plain = TmdbClient("fake-key", api_base="http://api.themoviedb.org/3", proxy=proxy, timeout=5.0)
            try:
                data = await plain._get("/tv/777", retries=1)
            finally:
                await plain.aclose()
            self.assertEqual(data["name"], "Proxied", "没拿到假代理返回的内容")
            self.assertTrue(seen[0].startswith("GET http://"), f"请求行不是绝对 URL：{seen[0]}")
            self.assertIn("api.themoviedb.org", seen[0])
        finally:
            server.close()
            await server.wait_closed()


if __name__ == "__main__":
    unittest.main()
