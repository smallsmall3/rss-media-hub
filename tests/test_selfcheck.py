"""部署自检测试。

自检的价值在于"把说不清的部署问题变成明确的下一步"，所以：
  * 每一项的判定要对（该 FAIL 就 FAIL、该 WARN 就 WARN）
  * 每条 FAIL/WARN 都要带 fix 提示（否则用户还是不知道怎么办）
  * 报告要能既给人看（文本）也给程序看（JSON）
"""

from __future__ import annotations

import contextlib
import itertools
import json
import os
import shutil
import stat
import unittest
from pathlib import Path

from app.config import Subscription, load_settings
from app.selfcheck import (
    FAIL,
    OK,
    WARN,
    CheckItem,
    SelfCheckReport,
    check_dir_writable,
    check_templates,
    run_selfcheck,
)

_counter = itertools.count()


@contextlib.contextmanager
def temp_dir():
    base = Path(os.environ.get("RMH_TEST_TMP") or Path(__file__).resolve().parent / ".tmp")
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"self-{os.getpid()}-{next(_counter)}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def make_settings(root: Path, *, subs_yaml: str | None = None, **env):
    cfg = root / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "subscriptions.yaml").write_text(
        subs_yaml
        if subs_yaml is not None
        else (
            "subscriptions:\n"
            "  - id: my-pt\n"
            "    name: 我的 PT 站\n"
            "    mode: feed\n"
            "    rss: https://pt.example/rss?passkey=X\n"
        ),
        encoding="utf-8",
    )
    st = root / "state"
    st.mkdir(parents=True, exist_ok=True)
    saved = {k: os.environ.get(k) for k in list(os.environ) if k.startswith("RMH_")}
    for key in list(os.environ):
        if key.startswith("RMH_"):
            os.environ.pop(key)
    os.environ.update({k: str(v) for k, v in env.items()})
    try:
        return load_settings(cfg, st)
    finally:
        for key in list(os.environ):
            if key.startswith("RMH_"):
                os.environ.pop(key)
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value


def find(report: SelfCheckReport, name: str) -> CheckItem | None:
    return next((i for i in report.items if name in i.name), None)


class ReportTest(unittest.TestCase):
    def test_status_counts(self):
        r = SelfCheckReport()
        r.add("a", OK, "fine")
        r.add("b", WARN, "meh", "试试这个")
        r.add("c", FAIL, "broken", "必须这样修")
        self.assertTrue(r.passed is False)
        self.assertEqual(len(r.failures), 1)
        self.assertEqual(len(r.warnings), 1)

    def test_passed_when_only_warnings(self):
        r = SelfCheckReport()
        r.add("a", OK)
        r.add("b", WARN, "meh", "fix")
        self.assertTrue(r.passed)

    def test_text_renders_fix_hints(self):
        r = SelfCheckReport()
        r.add("配置目录", FAIL, "不可写", "改 RMH_UID")
        text = r.render_text()
        self.assertIn("配置目录", text)
        self.assertIn("↳ 改 RMH_UID", text)
        self.assertIn("必须处理", text)
        self.assertIn("否则对应功能用不了", text)

    def test_text_all_ok(self):
        r = SelfCheckReport()
        r.add("a", OK, "好")
        self.assertIn("全部通过", r.render_text())

    def test_text_lists_failing_item_names(self):
        """结论里要点名是哪几项，别让人自己往上翻。"""
        r = SelfCheckReport()
        r.add("Telegram 配置", FAIL, "缺少 Token", "去 @BotFather 拿")
        r.add("订阅表", FAIL, "解析失败", "检查缩进")
        text = r.render_text()
        self.assertIn("有 2 项必须处理", text)
        self.assertIn("Telegram 配置", text)
        self.assertIn("订阅表", text)

    def test_json_shape(self):
        r = SelfCheckReport()
        r.add("a", OK, "好")
        r.add("b", FAIL, "坏", "修法")
        data = json.loads(r.render_json())
        self.assertFalse(data["ok"])
        self.assertEqual(data["failures"], 1)
        self.assertEqual(len(data["checks"]), 2)
        self.assertEqual(data["checks"][1]["fix"], "修法")


class DirWritableTest(unittest.TestCase):
    def test_writable_dir_is_ok(self):
        with temp_dir() as root:
            d = root / "config"
            d.mkdir()
            r = SelfCheckReport()
            check_dir_writable(r, d, "配置", "提示")
            self.assertEqual(r.items[-1].status, OK)

    def test_missing_dir_is_fail(self):
        with temp_dir() as root:
            r = SelfCheckReport()
            check_dir_writable(r, root / "nope", "配置", "检查挂载")
            self.assertEqual(r.items[-1].status, FAIL)
            self.assertIn("不存在", r.items[-1].detail)
            self.assertTrue(r.items[-1].fix)

    def test_readonly_dir_is_fail_with_uid_hint(self):
        if os.name == "nt":
            self.skipTest("Windows 上 chmod 语义不同，这个用例只在 Linux/macOS 有意义")
        with temp_dir() as root:
            sub = root / "ro"
            sub.mkdir()
            sub.chmod(stat.S_IRUSR | stat.S_IXUSR)
            try:
                r = SelfCheckReport()
                check_dir_writable(r, sub, "状态", "提示")
                last = r.items[-1]
                self.assertEqual(last.status, FAIL)
                self.assertIn("RMH_UID", last.fix, "不可写时必须给出改 UID 的具体办法")
            finally:
                sub.chmod(stat.S_IRWXU)

    def test_probe_file_cleaned_up(self):
        with temp_dir() as root:
            d = root / "probe"
            d.mkdir()
            r = SelfCheckReport()
            check_dir_writable(r, d, "配置", "x")
            self.assertEqual(list(d.iterdir()), [], "探测文件必须删掉，不能留在用户目录里")


class CheckTemplatesTest(unittest.TestCase):
    def test_real_entrypoint_passes(self):
        """项目自带的 entrypoint.sh 必须通过结构检查。"""
        r = SelfCheckReport()
        check_templates(r)
        item = find(r, "入口脚本")
        self.assertIsNotNone(item)
        self.assertEqual(item.status, OK, item.detail)
        tmpl = find(r, "首次启动模板")
        self.assertIsNotNone(tmpl)
        self.assertEqual(tmpl.status, OK, tmpl.detail)
        self.assertIn("条示例订阅", tmpl.detail)


class FullSelfCheckTest(unittest.TestCase):
    def test_healthy_setup_reports_no_failures(self):
        with temp_dir() as root:
            settings = make_settings(
                root,
                RMH_TG_BOT_TOKEN="111:abc",
                RMH_TG_CHAT_ID="-100123",
                RMH_TMDB_API_KEY="k",
                RMH_EMBY_URL="http://192.168.31.221:8096",
                RMH_EMBY_API_KEY="k",
                RMH_UI_TOKEN="s3cret",
            )
            report = run_selfcheck(settings)
            self.assertTrue(report.passed, report.render_text())
            self.assertEqual([i.name for i in report.failures], [])

    def test_missing_telegram_is_failure(self):
        with temp_dir() as root:
            settings = make_settings(root)
            report = run_selfcheck(settings)
            self.assertFalse(report.passed)
            item = find(report, "Telegram 配置")
            self.assertEqual(item.status, FAIL)
            self.assertIn("RMH_TG_BOT_TOKEN", item.detail)
            self.assertIn("@BotFather", item.fix)

    def test_missing_library_and_tmdb_are_warnings_only(self):
        """feed 全量模式不需要 Emby/TMDB，所以这两项不该算失败。"""
        with temp_dir() as root:
            settings = make_settings(root, RMH_TG_BOT_TOKEN="1:a", RMH_TG_CHAT_ID="1")
            report = run_selfcheck(settings)
            self.assertEqual(find(report, "Emby/Jellyfin 配置").status, WARN)
            self.assertEqual(find(report, "TMDB 配置").status, WARN)
            self.assertTrue(report.passed, "只缺 Emby/TMDB 不该判定为跑不起来")

    def test_unparsable_subscriptions_yaml_is_caught(self):
        """真正非法的 YAML 要被抓到并给出修法，而不是让自检崩掉。

        注意做法：先用正常文件建好 settings（load_settings 自己也会读，
        坏文件会让它先抛错），然后**再**把文件写坏，这样才能测到 selfcheck 的容错。
        """
        with temp_dir() as root:
            settings = make_settings(root)
            settings.subs_file.write_text(
                "subscriptions:\n  - id: a\n  name: 混在同一级\n", encoding="utf-8"
            )
            report = run_selfcheck(settings)   # 不能抛异常
            item = find(report, "订阅表")
            self.assertEqual(item.status, FAIL)
            self.assertIn("解析失败", item.detail)
            self.assertTrue(item.fix)

    def test_unknown_keys_are_tolerated(self):
        """未知字段应该被忽略而不是让自检失败——用户加注释/自定义字段很常见。"""
        with temp_dir() as root:
            settings = make_settings(
                root,
                subs_yaml=(
                    "subscriptions:\n"
                    "  - id: a\n"
                    "    name: 正常\n"
                    "    mode: feed\n"
                    "    rss: https://pt.example/rss\n"
                    "    随便写的字段: 123\n"
                ),
            )
            report = run_selfcheck(settings)
            self.assertEqual(find(report, "订阅表").status, OK)

    def test_show_subscription_without_tmdb_warns(self):
        with temp_dir() as root:
            settings = make_settings(
                root,
                subs_yaml=(
                    "subscriptions:\n"
                    "  - id: s\n"
                    "    name: 某剧\n"
                    "    mode: show\n"
                    "    rss: https://pt.example/rss\n"
                ),
            )
            report = run_selfcheck(settings)
            item = find(report, "TMDB ID")
            self.assertEqual(item.status, WARN)
            self.assertIn("某剧", item.detail)

    def test_enabled_subscription_without_rss_is_failure(self):
        with temp_dir() as root:
            settings = make_settings(
                root,
                subs_yaml="subscriptions:\n  - id: s\n    name: 缺RSS的\n    mode: show\n    tmdb_id: 1\n",
            )
            report = run_selfcheck(settings)
            item = find(report, "RSS 地址")
            self.assertEqual(item.status, FAIL)
            self.assertIn("缺RSS的", item.detail)

    def test_no_subscriptions_warns(self):
        with temp_dir() as root:
            settings = make_settings(root, subs_yaml="subscriptions: []\n")
            report = run_selfcheck(settings)
            self.assertEqual(find(report, "订阅表").status, WARN)

    def test_ui_without_token_warns(self):
        with temp_dir() as root:
            settings = make_settings(root)
            report = run_selfcheck(settings)
            item = find(report, "网页 UI")
            self.assertEqual(item.status, WARN)
            self.assertIn("RMH_UI_TOKEN", item.fix)

    def test_ui_disabled_is_ok(self):
        with temp_dir() as root:
            settings = make_settings(root, RMH_UI_ENABLED="false")
            report = run_selfcheck(settings)
            item = find(report, "网页 UI")
            self.assertEqual(item.status, OK)
            self.assertIn("已关闭", item.detail)

    def test_state_db_reported_when_present(self):
        with temp_dir() as root:
            settings = make_settings(root)
            settings.db_file.write_bytes(b"x" * 30000)
            report = run_selfcheck(settings)
            item = find(report, "状态数据库")
            self.assertEqual(item.status, OK)
            self.assertIn("KB", item.detail)

    def test_every_problem_has_a_fix(self):
        """所有 FAIL/WARN 都必须带「怎么办」，否则自检只是报丧。"""
        with temp_dir() as root:
            settings = make_settings(root, subs_yaml="subscriptions: []\n")
            report = run_selfcheck(settings)
            for item in report.items:
                if item.status in (FAIL, WARN):
                    self.assertTrue(item.fix, f"{item.name} 是 {item.status} 但没给 fix 提示")

    def test_report_is_offline(self):
        """自检不能依赖网络——断网时也要能跑（这里用"不产生网络调用"来间接验证）。"""
        with temp_dir() as root:
            settings = make_settings(root, RMH_TG_BOT_TOKEN="1:a", RMH_TG_CHAT_ID="1")
            report = run_selfcheck(settings)
            self.assertLess(report.elapsed, 2.0, "自检应该秒出，不该有网络等待")


class FastModeTest(unittest.TestCase):
    """预检必须"快速失败"——不然连不上时要等一分多钟，体验很差。

    背景：实测发现预检复用轮询参数（3 次重试 + 20 秒超时）时，
    三个服务全连不上要 85 秒。现在预检用 1 次重试 + 8 秒超时。
    """

    def test_tmdb_fast_context_switches_and_restores(self):
        from app.tmdb import TmdbClient

        client = TmdbClient("k", timeout=20.0, retries=3)
        self.assertEqual((client.retries, client.timeout), (3, 20.0))
        with client.fast(retries=1, timeout=8.0) as inner:
            self.assertIs(inner, client)
            self.assertEqual(client.retries, 1)
            self.assertEqual(client.timeout, 8.0)
        # 退出后必须还原，否则会影响后续轮询
        self.assertEqual((client.retries, client.timeout), (3, 20.0))

    def test_tmdb_fast_restores_even_on_exception(self):
        from app.tmdb import TmdbClient

        client = TmdbClient("k", timeout=20.0, retries=3)
        with self.assertRaises(RuntimeError):
            with client.fast(retries=1, timeout=5.0):
                raise RuntimeError("boom")
        self.assertEqual((client.retries, client.timeout), (3, 20.0))

    def test_preflight_probe_tolerates_client_without_fast(self):
        """假客户端没有 fast() 时也不能崩（要优雅降级）。"""
        import asyncio

        from app.selfcheck import _probe_tmdb_series

        class NoFast:
            def __init__(self):
                self.calls = 0

            async def series(self, name, tmdb_id=None, **kwargs):
                self.calls += 1
                return None

        client = NoFast()
        asyncio.run(_probe_tmdb_series(client, 1399, {"retries": 1, "timeout": 8.0}))
        self.assertEqual(client.calls, 1)


class PreflightTest(unittest.IsolatedAsyncioTestCase):
    """连通性预检：真的连外部服务，失败要给出可操作的修法。"""

    async def _make_hub(self, settings, *, tg=None, tmdb=None, emby=None, series_items=None):
        """构造一个只含预检所需能力的假 hub。"""

        class FakeTg:
            def __init__(self, behaviour):
                self.behaviour = behaviour
                self.sent: list[str] = []

            async def get_me(self):
                if isinstance(self.behaviour, Exception):
                    raise self.behaviour
                return {"username": "fake_bot", "first_name": "Fake"}

            async def send_message(self, text, **kwargs):
                from app.telegram import TgResult

                if isinstance(self.behaviour, Exception):
                    return TgResult(False, error=str(self.behaviour))
                self.sent.append(text)
                return TgResult(True, message_id=42)

        class FakeTmdb:
            def __init__(self, behaviour):
                self.behaviour = behaviour

            async def series(self, name, tmdb_id=None, **kwargs):
                if isinstance(self.behaviour, Exception):
                    raise self.behaviour
                from app.tmdb import SeriesInfo

                return SeriesInfo(tmdb_id=tmdb_id or 1, name="假剧", total_seasons=2, total_episodes=24)

            async def _get(self, path, params=None, retries=3):
                if isinstance(self.behaviour, Exception):
                    raise self.behaviour
                return {"images": {"base_url": "http://x"}}

        class FakeLibrary:
            def __init__(self, behaviour, items):
                self.behaviour = behaviour
                self._items = items

            async def ping(self):
                if isinstance(self.behaviour, Exception):
                    raise self.behaviour
                return {"ProductName": "Emby", "Version": "4.8"}

            async def detect_kind(self):
                return "emby"

            async def list_series(self, **kwargs):
                if isinstance(self.behaviour, Exception):
                    raise self.behaviour
                return list(self._items)

        hub = type("H", (), {})()
        hub.settings = settings
        hub.tg = FakeTg(tg if tg is not None else True)
        hub.tmdb = FakeTmdb(tmdb if tmdb is not None else True)
        hub.library = FakeLibrary(emby if emby is not None else True, series_items or [])
        return hub

    async def test_all_good_without_notify(self):
        from app.selfcheck import run_preflight

        with temp_dir() as root:
            settings = make_settings(
                root,
                RMH_TG_BOT_TOKEN="1:a",
                RMH_TG_CHAT_ID="-100",
                RMH_TMDB_API_KEY="k",
                RMH_EMBY_URL="http://emby.local:8096",
                RMH_EMBY_API_KEY="k",
            )
            settings.subscriptions = [Subscription(id="s", name="某剧", mode="show", tmdb_id=1399, rss="https://x/rss")]
            hub = await self._make_hub(settings, series_items=[{"Name": "某剧", "ProviderIds": {"Tmdb": "1399"}}])
            report = await run_preflight(hub, notify=False)
            self.assertTrue(report.passed, report.render_text())
            self.assertEqual(hub.tg.sent, [], "默认不该发送任何推送")
            text = report.render_text()
            self.assertIn("不会发送推送", text)
            self.assertIn("假剧", text)
            self.assertIn("2 季 / 24 集", text)
            self.assertIn("已刮削 TMDB ID", text)

    async def test_notify_sends_message(self):
        from app.selfcheck import run_preflight

        with temp_dir() as root:
            settings = make_settings(root, RMH_TG_BOT_TOKEN="1:a", RMH_TG_CHAT_ID="-100")
            hub = await self._make_hub(settings)
            report = await run_preflight(hub, notify=True)
            self.assertEqual(len(hub.tg.sent), 1, "加 --notify 时必须真的发一条")
            self.assertIn("预检消息", hub.tg.sent[0])
            self.assertEqual(find(report, "Telegram 推送").status, OK)

    async def test_telegram_failure_gives_proxy_hint(self):
        from app.selfcheck import run_preflight

        with temp_dir() as root:
            settings = make_settings(root, RMH_TG_BOT_TOKEN="1:a", RMH_TG_CHAT_ID="-100")
            hub = await self._make_hub(settings, tg=RuntimeError("ConnectTimeout"))
            report = await run_preflight(hub)
            item = find(report, "Telegram 连通")
            self.assertEqual(item.status, FAIL)
            self.assertIn("RMH_TG_PROXY", item.fix, "国内连不上 TG 时应该提示配代理")
            self.assertFalse(report.passed)

    async def test_telegram_send_failure_hint(self):
        from app.selfcheck import run_preflight
        from app.telegram import TgResult

        with temp_dir() as root:
            settings = make_settings(root, RMH_TG_BOT_TOKEN="1:a", RMH_TG_CHAT_ID="-100")

            class TgBadSend:
                async def get_me(self):
                    return {"username": "b", "first_name": "B"}

                async def send_message(self, text, **kwargs):
                    return TgResult(False, error="chat not found")

            hub = type("H", (), {})()
            hub.settings = settings
            hub.tg = TgBadSend()
            hub.tmdb = None
            hub.library = None
            report = await run_preflight(hub, notify=True)
            item = find(report, "Telegram 推送")
            self.assertEqual(item.status, FAIL)
            self.assertIn("chat id", item.fix)

    async def test_tmdb_failure_hint(self):
        from app.selfcheck import run_preflight
        from app.tmdb import TmdbError

        with temp_dir() as root:
            settings = make_settings(root, RMH_TMDB_API_KEY="k")
            settings.subscriptions = [Subscription(id="s", name="x", mode="show", tmdb_id=1, rss="https://x/rss")]
            hub = await self._make_hub(settings, tmdb=TmdbError("连接超时"))
            report = await run_preflight(hub)
            item = find(report, "TMDB 连通")
            self.assertEqual(item.status, FAIL)
            self.assertIn("RMH_TMDB_PROXY", item.fix)

    async def test_emby_failure_hint_mentions_localhost(self):
        from app.emby import EmbyError
        from app.selfcheck import run_preflight

        with temp_dir() as root:
            settings = make_settings(root, RMH_EMBY_URL="http://localhost:8096", RMH_EMBY_API_KEY="k")
            hub = await self._make_hub(settings, emby=EmbyError("连接被拒绝"))
            report = await run_preflight(hub)
            item = find(report, "媒体服务器连通")
            self.assertEqual(item.status, FAIL)
            self.assertIn("localhost", item.fix, "最常见的坑是把 localhost 写进容器配置")

    async def test_unconfigured_services_are_warnings(self):
        """feed 全量模式只需要 TG，缺 TMDB/Emby 不该算失败。"""
        from app.selfcheck import run_preflight

        with temp_dir() as root:
            settings = make_settings(root, RMH_TG_BOT_TOKEN="1:a", RMH_TG_CHAT_ID="-100")
            hub = await self._make_hub(settings)
            report = await run_preflight(hub)
            self.assertTrue(report.passed, report.render_text())
            self.assertEqual(find(report, "TMDB 连通").status, WARN)
            self.assertEqual(find(report, "媒体服务器连通").status, WARN)

    async def test_empty_library_is_warning(self):
        from app.selfcheck import run_preflight

        with temp_dir() as root:
            settings = make_settings(root, RMH_EMBY_URL="http://e:8096", RMH_EMBY_API_KEY="k")
            hub = await self._make_hub(settings, series_items=[])
            report = await run_preflight(hub)
            item = find(report, "媒体库可读")
            self.assertEqual(item.status, WARN)
            self.assertIn("一部剧都没读到", item.detail)

    async def test_subscription_without_rss_is_failure(self):
        from app.selfcheck import run_preflight

        with temp_dir() as root:
            settings = make_settings(root, RMH_TG_BOT_TOKEN="1:a", RMH_TG_CHAT_ID="1")
            settings.subscriptions = [Subscription(id="x", name="没RSS", mode="show", tmdb_id=1)]
            hub = await self._make_hub(settings)
            report = await run_preflight(hub)
            item = find(report, "订阅源")
            self.assertEqual(item.status, FAIL)
            self.assertIn("没RSS", item.detail)

    async def test_no_subscriptions_warns(self):
        from app.selfcheck import run_preflight

        with temp_dir() as root:
            settings = make_settings(root, subs_yaml="subscriptions: []\n", RMH_TG_BOT_TOKEN="1:a", RMH_TG_CHAT_ID="1")
            hub = await self._make_hub(settings)
            report = await run_preflight(hub)
            self.assertEqual(find(report, "订阅源").status, WARN)

    async def test_preflight_uses_fast_settings(self):
        """预检必须请求"快速失败"参数，否则连不上时要等一分多钟。"""
        from app.selfcheck import run_preflight

        with temp_dir() as root:
            settings = make_settings(root, RMH_TMDB_API_KEY="k", RMH_EMBY_URL="http://e:8096", RMH_EMBY_API_KEY="k")
            settings.subscriptions = [Subscription(id="s", name="x", mode="show", tmdb_id=1, rss="https://x/rss")]

            seen: dict[str, Any] = {}

            class TmdbWithFast:
                def __init__(self):
                    self.retries, self.timeout = 3, 20.0

                def fast(self, **kwargs):
                    import contextlib

                    @contextlib.contextmanager
                    def cm():
                        seen["tmdb_fast"] = dict(kwargs)
                        old = (self.retries, self.timeout)
                        self.retries, self.timeout = kwargs.get("retries", 1), kwargs.get("timeout", 8.0)
                        try:
                            yield self
                        finally:
                            self.retries, self.timeout = old

                    return cm()

                async def series(self, name, tmdb_id=None, **kwargs):
                    from app.tmdb import SeriesInfo

                    seen["tmdb_retries_at_call"] = self.retries
                    seen["tmdb_timeout_at_call"] = self.timeout
                    return SeriesInfo(tmdb_id=1, name="剧", total_seasons=1, total_episodes=1)

            class LibWithFast:
                def __init__(self):
                    self.timeout = 20.0

                def fast(self, **kwargs):
                    import contextlib

                    @contextlib.contextmanager
                    def cm():
                        seen["lib_fast"] = dict(kwargs)
                        old = self.timeout
                        self.timeout = kwargs.get("timeout", 5.0)
                        try:
                            yield self
                        finally:
                            self.timeout = old

                    return cm()

                async def ping(self):
                    seen["lib_timeout_at_call"] = self.timeout
                    return {"ProductName": "Emby", "Version": "4.9"}

                async def detect_kind(self):
                    return "emby"

                async def list_series(self, **kwargs):
                    return [{"Name": "剧", "ProviderIds": {"Tmdb": "1"}}]

            hub = type("H", (), {})()
            hub.settings = settings
            hub.tg = type("T", (), {"get_me": lambda self=None: None})()
            hub.tmdb = TmdbWithFast()
            hub.library = LibWithFast()

            async def no_tg():
                return {"username": "b", "first_name": "B"}

            hub.tg = type("T", (), {})()
            hub.tg.get_me = no_tg

            await run_preflight(hub, notify=False)

            self.assertIn("tmdb_fast", seen, "预检没有请求 TMDB 的快速模式")
            self.assertEqual(seen["tmdb_fast"].get("retries"), 1)
            self.assertEqual(seen["tmdb_fast"].get("timeout"), 8.0)
            self.assertEqual(seen["tmdb_retries_at_call"], 1, "调用时应该已经切到 1 次重试")
            self.assertEqual(seen["tmdb_timeout_at_call"], 8.0)
            self.assertIn("lib_fast", seen, "预检没有请求媒体服务器的快速模式")
            self.assertEqual(seen["lib_fast"].get("timeout"), 5.0)
            self.assertEqual(seen["lib_timeout_at_call"], 5.0)

    async def test_preflight_problems_have_fixes(self):
        from app.selfcheck import run_preflight

        with temp_dir() as root:
            settings = make_settings(root)
            hub = await self._make_hub(settings)
            report = await run_preflight(hub)
            for item in report.items:
                if item.status in (FAIL, WARN):
                    self.assertTrue(item.fix, f"{item.name} 是 {item.status} 但没给 fix")


if __name__ == "__main__":
    unittest.main()
