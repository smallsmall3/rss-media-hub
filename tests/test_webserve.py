"""网页 UI / HTTP API 测试。

每个用例都真的起一个 asyncio TCP 服务器，然后用 asyncio.open_connection 发
真实 HTTP 请求 —— 连路由、鉴权、状态码、JSON 编码一起验证，
比只调 handler 函数可靠得多。
"""

from __future__ import annotations

import contextlib
import itertools
import json
import os
import shutil
import unittest
from pathlib import Path
from typing import Any

from app.config import Subscription, apply_overrides, load_settings
from app.main import Hub
from app.webserve import WebUI, env_overridden, mask

_counter = itertools.count()


@contextlib.contextmanager
def temp_dir():
    base = Path(os.environ.get("RMH_TEST_TMP") or Path(__file__).resolve().parent / ".tmp")
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"web-{os.getpid()}-{next(_counter)}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class HttpResponse:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")


async def request(port: int, method: str, path: str, body: Any = None, token: str = "") -> HttpResponse:
    """发一个真实 HTTP 请求到本地测试服务器。"""
    import asyncio

    payload = b""
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = [f"{method} {path} HTTP/1.1", "Host: 127.0.0.1", "Connection: close"]
    if payload:
        headers.append("Content-Type: application/json")
        headers.append(f"Content-Length: {len(payload)}")
    if token:
        headers.append(f"X-RMH-Token: {token}")
    raw = ("\r\n".join(headers) + "\r\n\r\n").encode("utf-8") + payload

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(raw)
        await writer.drain()
        data = await asyncio.wait_for(reader.read(), timeout=15)
    finally:
        with contextlib.suppress(Exception):
            writer.close()
    head, _, rest = data.partition(b"\r\n\r\n")
    lines = head.decode("utf-8", "replace").split("\r\n")
    status = int(lines[0].split()[1]) if lines and len(lines[0].split()) > 1 else 0
    hdrs = {}
    for line in lines[1:]:
        if ":" in line:
            k, _, v = line.partition(":")
            hdrs[k.strip().lower()] = v.strip()
    return HttpResponse(status, hdrs, rest)


class MaskTest(unittest.TestCase):
    def test_mask_keeps_ends(self):
        self.assertEqual(mask("8283147704:AAGq_hcX9n17KeLlkg5"), "8283********lkg5")
        self.assertEqual(mask(""), "")
        self.assertEqual(mask("abc"), "***")  # 太短就全打码

    def test_env_overridden_detects(self):
        os.environ["RMH_TMDB_API_KEY"] = "x"
        try:
            found = env_overridden()
        finally:
            os.environ.pop("RMH_TMDB_API_KEY", None)
        self.assertIn("tmdb.api_key", found)
        self.assertEqual(found["tmdb.api_key"], "RMH_TMDB_API_KEY")


class OverridesTest(unittest.TestCase):
    def test_apply_and_reload(self):
        with temp_dir() as root:
            cfg = root / "config"
            cfg.mkdir(parents=True)
            (cfg / "config.yaml").write_text(
                "telegram:\n  chat_id: from-config\n  send_poster: true\n", encoding="utf-8"
            )
            (cfg / "subscriptions.yaml").write_text("subscriptions: []\n", encoding="utf-8")

            # 只有 config.yaml 时
            s = load_settings(cfg, root / "state")
            self.assertEqual(s.telegram.chat_id, "from-config")

            # UI 写入覆盖
            result = apply_overrides(cfg, changes={"telegram": {"chat_id": "from-ui", "send_poster": False}})
            self.assertEqual(result["ignored"], [])
            self.assertTrue((cfg / "settings.yaml").exists())

            s2 = load_settings(cfg, root / "state")
            self.assertEqual(s2.telegram.chat_id, "from-ui", "UI 的值应该覆盖 config.yaml")
            self.assertFalse(s2.telegram.send_poster)

            # 环境变量优先级最高
            os.environ["RMH_TG_CHAT_ID"] = "from-env"
            try:
                s3 = load_settings(cfg, root / "state")
            finally:
                os.environ.pop("RMH_TG_CHAT_ID", None)
            self.assertEqual(s3.telegram.chat_id, "from-env")

    def test_whitelist_rejects_unknown(self):
        with temp_dir() as root:
            cfg = root / "config"
            cfg.mkdir(parents=True)
            result = apply_overrides(
                cfg,
                changes={
                    "telegram": {"bot_token": "abc", "evil_field": "boom"},
                    "runtime": {"poll_interval": 60},
                    "hacker": {"x": 1},
                },
            )
            self.assertIn("telegram.evil_field", result["ignored"])
            self.assertIn("hacker.x", result["ignored"])
            self.assertEqual(result["settings"]["telegram"]["bot_token"], "abc")
            self.assertNotIn("evil_field", result["settings"]["telegram"])
            self.assertEqual(result["settings"]["runtime"]["poll_interval"], 60)

    def test_empty_string_clears_value(self):
        with temp_dir() as root:
            cfg = root / "config"
            cfg.mkdir(parents=True)
            apply_overrides(cfg, changes={"tmdb": {"api_key": "secret"}})
            apply_overrides(cfg, changes={"tmdb": {"api_key": ""}})
            s = load_settings(cfg, root / "state")
            self.assertEqual(s.tmdb.api_key, "")

    def test_bool_and_int_coercion(self):
        with temp_dir() as root:
            cfg = root / "config"
            cfg.mkdir(parents=True)
            # 表单/JSON 传来的是字符串和数字混着，都要能吃
            result = apply_overrides(
                cfg,
                changes={"library": {"count_aired_only": "false", "verify_tls": True}, "runtime": {"poll_interval": "1200"}},
            )
            self.assertIs(result["settings"]["library"]["count_aired_only"], False)
            self.assertIs(result["settings"]["library"]["verify_tls"], True)
            self.assertEqual(result["settings"]["runtime"]["poll_interval"], 1200)
            s = load_settings(cfg, root / "state")
            self.assertFalse(s.library.count_aired_only)
            self.assertEqual(s.poll_interval, 1200)


class WebUITest(unittest.TestCase):
    """界面（HTML/CSS/JS）的结构回归：这些东西靠人工看容易漏。"""

    def setUp(self):
        from app.webui import INDEX_HTML

        self.html = INDEX_HTML
        self.js = INDEX_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
        self.css = INDEX_HTML.split("<style>", 1)[1].split("</style>", 1)[0]

    def test_brackets_balanced(self):
        for name, text in (("JS", self.js), ("CSS", self.css)):
            for op, cl in (("{", "}"), ("(", ")"), ("[", "]")):
                if name == "CSS" and op != "{":
                    continue
                self.assertEqual(text.count(op), text.count(cl), f"{name} 里 {op}{cl} 不配对")

    def test_no_bare_td_cells(self):
        """移动端把表格变卡片，靠 data-label；漏一个就会显示出光秃秃的 L 字。"""
        import re

        bare = re.findall(r"<td(?![^>]*data-label)", self.js)
        self.assertEqual(bare, [], "有单元格没有 data-label，移动端会显示异常")

    def test_no_cdn_or_external_assets(self):
        """必须彻底自包含：NAS 上常常访问不了外网，不能依赖任何 CDN。"""
        for bad in ("cdn.", "unpkg", "jsdelivr", "googleapis", "bootstrap"):
            self.assertNotIn(bad, self.html.lower(), f"不能依赖外部资源：{bad}")
        self.assertNotIn("<link", self.html.lower(), "不该有外部样式表引用")
        self.assertNotIn("src=\"http", self.html)

    def test_theme_toggle_present(self):
        self.assertIn("data-theme", self.html)
        self.assertIn("toggleTheme", self.js)
        self.assertIn("rmh_theme", self.js, "主题选择要记住（localStorage）")
        self.assertIn('html[data-theme="light"]', self.css, "浅色模式变量缺失")

    def test_tabs_present(self):
        for tab in ("dash", "subs", "scan", "settings"):
            self.assertIn(f'data-tab="{tab}"', self.html)
            self.assertIn(f'id="tab-{tab}"', self.html)

    def test_key_features_wired(self):
        # 查漏 / 扫描 / 配置 / 订阅编辑 / TG 测试 的入口都在
        for marker in ("/api/gaps", "/api/scan", "/api/config", "/api/subscriptions", "/api/telegram/test"):
            self.assertIn(marker, self.js, f"界面上缺少 {marker} 的调用")
        for fn in ("runGaps", "runScan", "saveConfig", "editSub", "testTg"):
            self.assertIn(f'function {fn}' if fn != "editSub" else "function editSub", self.js)

    def test_mode_specific_columns(self):
        """feed 与 show 两种模式在界面上要有不同呈现。"""
        self.assertIn("全量转发", self.js)
        self.assertIn("按剧追踪", self.js)
        self.assertIn("不比对媒体库", self.js)

    def test_scan_view_has_status_filters(self):
        """扫描结果要能按状态筛选（看"需处理"的未匹配剧，而不是只看缺集）。"""
        for marker in ("f-all", "f-gap", "f-empty", "f-attn", "f-done"):
            self.assertIn(marker, self.js, f"缺少状态筛选：{marker}")
        self.assertIn("statusPill", self.js)
        self.assertIn("未匹配", self.js)
        self.assertIn("需处理", self.js)

    def test_dark_theme_is_default(self):
        self.assertIn("color-scheme:dark", self.css)
        # 默认（未保存偏好且系统非浅色）应落在 dark
        self.assertIn("prefersLight ? 'light' : 'dark'", self.js)

    def test_html_ends_cleanly(self):
        self.assertTrue(self.html.rstrip().endswith("</html>"))


class WebApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        os.environ.update(
            {
                "RMH_TG_BOT_TOKEN": "111:SECRET_TOKENSECRET",
                "RMH_TG_CHAT_ID": "-100999",
                "RMH_TG_SEND_POSTER": "false",
                "RMH_TMDB_API_KEY": "SECRET_TMDB_KEY_1234",
                "RMH_EMBY_URL": "http://emby.example:8096",
                "RMH_EMBY_API_KEY": "SECRET_EMBY_KEY_5678",
                "RMH_UI_TOKEN": "",
                "RMH_HEALTH_PORT": "0",
                "RMH_LOG_LEVEL": "CRITICAL",
                # 固定住轮询间隔，避免宿主机/CI 上残留的 RMH_POLL_INTERVAL 影响断言
                "RMH_POLL_INTERVAL": "900",
            }
        )
        self._hubs: list[Hub] = []
        self._port = 0

    async def asyncTearDown(self) -> None:
        if self._ui is not None:
            await self._ui.stop()
        for hub in self._hubs:
            await hub.aclose()
        for key in list(os.environ):
            if key.startswith("RMH_"):
                os.environ.pop(key, None)

    _ui: WebUI | None = None

    async def _start(self, root: Path, *, token: str = "", subs_yaml: str | None = None) -> Hub:
        cfg = root / "config"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "subscriptions.yaml").write_text(
            subs_yaml
            or (
                "subscriptions:\n"
                "  - id: demo\n"
                "    name: 示例剧\n"
                "    tmdb_id: 1396\n"
                "    rss: https://pt.example/rss?passkey=SECRET\n"
            ),
            encoding="utf-8",
        )
        settings = load_settings(cfg, root / "state")
        hub = Hub(settings)
        self._hubs.append(hub)
        self._ui = WebUI(hub, host="127.0.0.1", port=0, token=token)
        await self._ui.start()
        self._port = self._ui.bound_port
        return hub

    # ---------------------------------------------------------------
    async def test_healthz_needs_no_token(self):
        with temp_dir() as root:
            await self._start(root, token="s3cret")
            res = await request(self._port, "GET", "/healthz")
            self.assertEqual(res.status, 200)
            self.assertEqual(res.text, "ok")

    async def test_auth_required_when_token_set(self):
        with temp_dir() as root:
            await self._start(root, token="s3cret")
            res = await request(self._port, "GET", "/api/dashboard")
            self.assertEqual(res.status, 401)
            self.assertIn("口令", res.json["error"])

            res = await request(self._port, "GET", "/api/dashboard", token="s3cret")
            self.assertEqual(res.status, 200)
            self.assertTrue(res.json["ok"])

            # 也支持 ?token=
            res = await request(self._port, "GET", "/api/dashboard?token=s3cret")
            self.assertEqual(res.status, 200)

            res = await request(self._port, "GET", "/api/dashboard", token="wrong")
            self.assertEqual(res.status, 401)

    async def test_dashboard_shape(self):
        with temp_dir() as root:
            await self._start(root)
            res = await request(self._port, "GET", "/api/dashboard")
            data = res.json
            self.assertEqual(res.status, 200)
            self.assertTrue(data["ok"])
            self.assertEqual(len(data["subscriptions"]), 1)
            sub = data["subscriptions"][0]
            self.assertEqual(sub["id"], "demo")
            self.assertIn("progress", sub)
            self.assertTrue(data["health"]["telegram"])
            self.assertTrue(data["health"]["tmdb"])
            self.assertEqual(data["stats"]["notifications"], 0)

    async def test_config_masks_secrets(self):
        with temp_dir() as root:
            await self._start(root)
            res = await request(self._port, "GET", "/api/config")
            data = res.json
            raw = res.text

            self.assertTrue(data["values"]["telegram"]["bot_token"]["set"])
            self.assertNotIn("SECRET_TOKENSECRET", raw, "密钥原文绝不能出现在响应里")
            self.assertIn("111:", data["values"]["telegram"]["bot_token"]["masked"])
            self.assertNotIn("SECRET_TMDB_KEY_1234", raw)
            self.assertNotIn("SECRET_EMBY_KEY_5678", raw)

            # 非密钥字段照常明文返回
            self.assertEqual(data["values"]["telegram"]["chat_id"], "-100999")
            # 被环境变量覆盖的字段要标出来
            self.assertIn("tmdb.api_key", data["env_overridden"])
            self.assertEqual(data["env_overridden"]["tmdb.api_key"], "RMH_TMDB_API_KEY")

    async def test_config_post_never_echoes_plaintext_secrets(self):
        """保存密钥后，响应里绝不能回明文。

        这是真实踩过的坑：apply_overrides 返回的是**明文**配置，
        POST 处理器把它当 `saved` 字段原样回给浏览器 —— 等于把你刚填的
        token 又送回来一次，抓包、代理日志、浏览器历史里都会留下。
        GET 一直有 mask，POST 漏了，所以两个都要断言。
        """
        with temp_dir() as root:
            # 先清掉环境变量，免得被覆盖后测不到
            os.environ.pop("RMH_TG_BOT_TOKEN", None)
            os.environ.pop("RMH_TMDB_API_KEY", None)
            os.environ.pop("RMH_EMBY_API_KEY", None)
            os.environ.pop("RMH_UI_TOKEN", None)
            hub = await self._start(root)

            secrets = {
                "telegram": {"bot_token": "111:PLAINTEXT_TG_TOKEN", "chat_id": "-100888"},
                "tmdb": {"api_key": "PLAINTEXT_TMDB_KEY"},
                "library": {"api_key": "PLAINTEXT_EMBY_KEY"},
                "ui": {"token": "PLAINTEXT_UI_TOKEN"},
            }
            res = await request(self._port, "POST", "/api/config", {"changes": secrets})
            self.assertEqual(res.status, 200, res.text)

            # 1) 功能必须仍然正常：密钥真的保存并生效了
            self.assertEqual(hub.settings.telegram.bot_token, "111:PLAINTEXT_TG_TOKEN")
            self.assertEqual(hub.settings.tmdb.api_key, "PLAINTEXT_TMDB_KEY")
            self.assertEqual(hub.settings.library.api_key, "PLAINTEXT_EMBY_KEY")
            self.assertEqual(hub.settings.ui_token, "PLAINTEXT_UI_TOKEN")

            # 2) 响应里一个字都不能出现
            for name in ("PLAINTEXT_TG_TOKEN", "PLAINTEXT_TMDB_KEY",
                         "PLAINTEXT_EMBY_KEY", "PLAINTEXT_UI_TOKEN"):
                self.assertNotIn(name, res.text, f"POST 响应泄露了 {name}")

            # 3) 但要能看出"已设置"和打码预览
            saved = res.json["saved"]
            self.assertTrue(saved["telegram"]["bot_token"]["set"])
            self.assertIn("****", saved["telegram"]["bot_token"]["masked"])
            self.assertNotEqual(saved["telegram"]["bot_token"]["masked"], "111:PLAINTEXT_TG_TOKEN")
            # 非密钥字段该明文就明文，方便确认填对了
            self.assertEqual(saved["telegram"]["chat_id"], "-100888")

            # 4) 紧接着 GET 一次也不能泄露（overrides 字段是文件原始内容）
            res2 = await request(self._port, "GET", "/api/config")
            for name in ("PLAINTEXT_TG_TOKEN", "PLAINTEXT_TMDB_KEY",
                         "PLAINTEXT_EMBY_KEY", "PLAINTEXT_UI_TOKEN"):
                self.assertNotIn(name, res2.text, f"GET 响应泄露了 {name}")

    async def test_config_post_rejects_wrong_payload_shape(self):
        """漏了 changes 这一层要明确报错，而不是静默什么都不做。"""
        with temp_dir() as root:
            await self._start(root)
            res = await request(self._port, "POST", "/api/config", {"telegram": {"chat_id": "-1"}})
            self.assertEqual(res.status, 400)
            self.assertIn("changes", res.json["error"])

    async def test_config_post_saves_and_hot_reloads(self):
        with temp_dir() as root:
            hub = await self._start(root)
            res = await request(
                self._port,
                "POST",
                "/api/config",
                {"changes": {"runtime": {"poll_interval": 300}, "library": {"count_aired_only": False}}},
            )
            self.assertEqual(res.status, 200, res.text)
            self.assertTrue(res.json["ok"])

            # 立刻热重载：环境变量优先，所以这里改成验证"UI 保存后 settings.yaml 有值、
            # 且被环境变量覆盖的字段仍以环境变量为准"
            self.assertTrue((root / "config" / "settings.yaml").exists())
            fresh = load_settings(root / "config", root / "state")
            self.assertFalse(fresh.library.count_aired_only, "UI 保存的 library 设置应生效")
            import yaml as _yaml

            saved = _yaml.safe_load((root / "config" / "settings.yaml").read_text(encoding="utf-8"))
            self.assertEqual(saved["runtime"]["poll_interval"], 300, "UI 的值应写进 settings.yaml")

    async def test_config_post_value_overrides_file_without_env(self):
        """没有环境变量时，UI 保存的值应当真的改变运行时配置。"""
        with temp_dir() as root:
            hub = await self._start(root)
            os.environ.pop("RMH_POLL_INTERVAL", None)
            res = await request(
                self._port,
                "POST",
                "/api/config",
                {"changes": {"runtime": {"poll_interval": 240, "scan_concurrency": 3}}},
            )
            self.assertEqual(res.status, 200, res.text)
            self.assertEqual(hub.settings.poll_interval, 240, "保存后应立刻热重载")
            self.assertEqual(hub.settings.scan_concurrency, 3)
            # 响应里要回显"真的应用到内存里的值"，而不是只回显写进文件的内容
            self.assertEqual(res.json["applied"]["poll_interval"], 240)
            self.assertEqual(res.json["applied"]["scan_concurrency"], 3)

            # 客户端对象被重建了（换 Token / 换地址必须重建）
            import app.tmdb as tmdb_mod

            self.assertIsInstance(hub.tmdb, tmdb_mod.TmdbClient)

    async def test_config_hot_reload_switches_emby_target(self):
        """改 Emby 地址后，新建的客户端应指向新地址（不需要重启容器）。"""
        with temp_dir() as root:
            hub = await self._start(root)
            os.environ.pop("RMH_EMBY_URL", None)
            old_client = hub.library
            res = await request(
                self._port, "POST", "/api/config", {"changes": {"library": {"url": "http://new-emby.local:8096"}}}
            )
            self.assertEqual(res.status, 200, res.text)
            self.assertIsNot(hub.library, old_client, "应该重建了 Emby 客户端")
            self.assertEqual(hub.library.url, "http://new-emby.local:8096")
            self.assertEqual(hub.scanner.library, hub.library, "扫描器也要指向新客户端")
            self.assertEqual(res.json["applied"]["library_url"], "http://new-emby.local:8096")

    async def test_config_post_rejects_bad_payload(self):
        with temp_dir() as root:
            await self._start(root)
            res = await request(self._port, "POST", "/api/config", {"nope": 1})
            self.assertEqual(res.status, 400)
            self.assertIn("changes", res.json["error"])

            res = await request(self._port, "POST", "/api/config", {"changes": {}})
            self.assertEqual(res.status, 400)

    async def test_subscriptions_crud(self):
        with temp_dir() as root:
            hub = await self._start(
                root,
                subs_yaml=(
                    "subscriptions:\n"
                    "  - id: demo\n"
                    "    name: 示例剧\n"
                    "    tmdb_id: 1396\n"
                    "    rss: https://pt.example/rss?passkey=SECRET\n"
                ),
            )

            res = await request(self._port, "GET", "/api/subscriptions")
            self.assertEqual(len(res.json["subscriptions"]), 1)

            # 新增
            res = await request(
                self._port,
                "POST",
                "/api/subscriptions",
                {"subscription": {"name": "新剧", "tmdb_id": 999, "rss": "https://pt.example/rss2", "quality": ["1080p"]}},
            )
            self.assertEqual(res.status, 200, res.text)
            new_id = res.json["subscription"]["id"]
            self.assertEqual(len(hub.settings.subscriptions), 2)
            self.assertIn(new_id, hub.runtimes, "新订阅应该立刻进入轮询计划")

            # 落盘检查
            text = (root / "config" / "subscriptions.yaml").read_text(encoding="utf-8")
            self.assertIn("新剧", text)

            # 重复 id 应被拒绝
            res = await request(
                self._port, "POST", "/api/subscriptions", {"subscription": {"id": new_id, "name": "新剧", "tmdb_id": 1}}
            )
            self.assertEqual(res.status, 400)
            self.assertIn("已存在", res.json["error"])

            # 缺字段应被拒绝
            res = await request(self._port, "POST", "/api/subscriptions", {"subscription": {}})
            self.assertEqual(res.status, 400)

            # 编辑（带 overwrite）
            res = await request(
                self._port,
                "POST",
                "/api/subscriptions",
                {"overwrite": True, "subscription": {"id": new_id, "name": "新剧改名", "tmdb_id": 999, "rss": "https://pt.example/rss2"}},
            )
            self.assertEqual(res.status, 200)
            self.assertIn("新剧改名", {s.name for s in hub.settings.subscriptions})

            # 删除
            res = await request(self._port, "DELETE", f"/api/subscriptions?id={new_id}")
            self.assertEqual(res.status, 200, res.text)
            self.assertEqual(len(hub.settings.subscriptions), 1)
            self.assertNotIn(new_id, hub.runtimes)

            # 删不存在的
            res = await request(self._port, "DELETE", "/api/subscriptions?id=ghost")
            self.assertEqual(res.status, 400)

    async def test_status_endpoint(self):
        with temp_dir() as root:
            # /api/status 读的是数据库里的巡检状态，所以先做一次比对把它写进去
            hub = await self._start(root)
            sub = next(iter(hub.runtimes.values())).sub

            async def fake_series(*args: Any, **kwargs: Any):
                from app.tmdb import EpisodeInfo, SeasonInfo, SeriesInfo

                return SeriesInfo(
                    tmdb_id=1396, name="示例剧", year=2020, total_seasons=1, total_episodes=2,
                    seasons=[SeasonInfo(number=1, episode_count=2, episodes=[
                        EpisodeInfo(season=1, episode=1, air_date=None),
                        EpisodeInfo(season=1, episode=2, air_date=None),
                    ])],
                )

            async def fake_find(name: str, **kwargs: Any):
                from app.emby import LocalEpisode, LocalSeries

                local = LocalSeries(item_id="s1", name="示例剧", provider_ids={"Tmdb": "1396"})
                local.episodes = [LocalEpisode(season=1, episode=1)]
                return local

            hub.tmdb.series = fake_series  # type: ignore[assignment]
            hub.library.find_series = fake_find  # type: ignore[assignment]
            await hub.reconcile_subscription(sub, notify=False)

            res = await request(self._port, "GET", "/api/status")
            self.assertEqual(res.status, 200)
            row = res.json["subscriptions"][0]
            self.assertEqual(row["id"], "demo")
            self.assertEqual((row["owned"], row["total"]), (1, 2))
            self.assertEqual(row["missing"], "S01E02")

    async def test_telegram_test_reports_missing_config(self):
        with temp_dir() as root:
            await self._start(root)
            os.environ.pop("RMH_TG_BOT_TOKEN", None)
            hub = self._hubs[-1]
            hub.settings.telegram.bot_token = ""
            res = await request(self._port, "POST", "/api/telegram/test", {})
            self.assertEqual(res.status, 400)
            self.assertIn("Telegram", res.json["error"])

    async def test_gaps_endpoint_cross_checks_feed_against_library(self):
        """查漏：先把库扫出缺口，再和 RSS 里的条目对上。"""
        from app import gapfill as gapfill_mod
        from app.libraryscan import STATUS_PARTIAL, ScanResult, SeriesScan
        from app.rss import FeedItem

        with temp_dir() as root:
            hub = await self._start(
                root,
                subs_yaml=(
                    "subscriptions:\n"
                    "  - id: pt\n"
                    "    name: 我的源\n"
                    "    mode: feed\n"
                    "    rss: https://pt.example/rss?passkey=SECRET\n"
                ),
            )

            # 库里有一部缺 S01E04/S01E05 的剧
            scan = ScanResult(library_total=1, scanned=1, finished_at=0.0)
            scan.series = [
                SeriesScan(
                    "s1", "缺集剧", tmdb_id=42, total=6, aired=6, owned=4,
                    status=STATUS_PARTIAL, missing_codes=["S01E04", "S01E05"],
                )
            ]

            async def fake_scan(*args: Any, **kwargs: Any) -> ScanResult:
                return scan

            hub.scanner.scan = fake_scan  # type: ignore[method-assign]

            # RSS 里只有 E05
            async def fake_fetch(client: Any, url: str, **kwargs: Any) -> list[FeedItem]:
                return [
                    FeedItem(
                        title="缺集剧 S01E05 [1080p]",
                        link="https://pt.example/details?id=5",
                        download_url="https://pt.example/download.php?id=5&passkey=SECRET",
                        guid="pt-5",
                        season=1,
                        episode=5,
                        size_bytes=2_000_000_000,
                    )
                ]

            original = gapfill_mod.fetch_feed
            gapfill_mod.fetch_feed = fake_fetch
            try:
                res = await request(self._port, "POST", "/api/gaps", {})
            finally:
                gapfill_mod.fetch_feed = original

            self.assertEqual(res.status, 200, res.text)
            data = res.json
            self.assertEqual(data["summary"]["feed_items"], 1)
            self.assertEqual(data["summary"]["total_missing"], 2)
            self.assertEqual(data["summary"]["total_covered"], 1)

            series = data["result"]["series"][0]
            self.assertEqual(series["display_name"], "缺集剧")
            gaps = {g["code"]: g for g in series["gaps"]}
            self.assertTrue(gaps["S01E05"]["available"])
            self.assertIn("passkey=SECRET", gaps["S01E05"]["best"]["download_url"])
            self.assertFalse(gaps["S01E04"]["available"])
            # 规格标签要一起返回，界面才能显示"2160p · HDR"这种
            self.assertIn("1080p", gaps["S01E05"]["best"]["badges"])
            self.assertEqual(gaps["S01E04"]["best"], None)

            # 第二次应该复用上次扫描结果，不重复扫库
            scan_calls = {"n": 0}

            async def counting_scan(*args: Any, **kwargs: Any) -> ScanResult:
                scan_calls["n"] += 1
                return scan

            hub.scanner.scan = counting_scan  # type: ignore[method-assign]
            gapfill_mod.fetch_feed = fake_fetch
            try:
                res2 = await request(self._port, "POST", "/api/gaps", {})
            finally:
                gapfill_mod.fetch_feed = original
            self.assertTrue(res2.json["reused_scan"], "第二次应复用扫描结果")
            self.assertEqual(scan_calls["n"], 0, "不该重复扫库")

            # rescan=true 时应该真的重扫
            gapfill_mod.fetch_feed = fake_fetch
            try:
                res3 = await request(self._port, "POST", "/api/gaps", {"rescan": True})
            finally:
                gapfill_mod.fetch_feed = original
            self.assertFalse(res3.json["reused_scan"])
            self.assertEqual(scan_calls["n"], 1)

    async def test_gaps_endpoint_requires_config(self):
        with temp_dir() as root:
            hub = await self._start(root)
            # 没有订阅
            hub.settings.subscriptions = []
            res = await request(self._port, "POST", "/api/gaps", {})
            self.assertEqual(res.status, 400)
            self.assertIn("订阅", res.json["error"])

            # 没有 Emby
            hub.settings.subscriptions = [Subscription(id="x", name="x", mode="feed", rss="https://x/rss")]
            hub.settings.library.api_key = ""
            res = await request(self._port, "POST", "/api/gaps", {})
            self.assertEqual(res.status, 400)
            self.assertIn("Emby", res.json["error"])

    async def test_selfcheck_endpoint(self):
        """离线自检接口。"""
        with temp_dir() as root:
            await self._start(root)
            res = await request(self._port, "GET", "/api/selfcheck")
            self.assertEqual(res.status, 200, res.text)
            data = res.json
            self.assertIn("passed", data)
            self.assertGreater(len(data["result"]["checks"]), 5)
            self.assertIn("部署自检报告", data["text"])

    async def test_preflight_endpoint_reports_unreachable_without_crashing(self):
        """预检会真的发请求；服务不可达时要返回 200 + 可读报告，而不是 500。

        注意：必须**先用不可达地址构造好 hub**，因为客户端对象是在
        _build_services 里按当时的配置建出来的；事后改 settings 不会生效
        （这正是热重载需要重建客户端的原因）。
        """
        with temp_dir() as root:
            os.environ.pop("RMH_EMBY_URL", None)
            os.environ.pop("RMH_TG_API_BASE", None)
            os.environ.pop("RMH_TMDB_API_BASE", None)
            hub = await self._start(root)
            hub.settings.telegram.bot_token = "111:FAKE"
            hub.settings.telegram.chat_id = "-1"
            # 把三个服务都指向本机一个没人监听的端口，避免真的打外网
            hub.settings.telegram.api_base = "http://127.0.0.1:9"
            hub.settings.tmdb.api_key = "fake"
            hub.settings.tmdb.api_base = "http://127.0.0.1:9"
            hub.settings.tmdb.retries = 1
            hub.settings.tmdb.timeout = 1.0
            hub.settings.library.url = "http://127.0.0.1:9"
            hub.settings.library.api_key = "fake"
            # 改完 settings 必须重建客户端，否则预检打的还是旧地址
            hub._build_services()

            res = await request(self._port, "POST", "/api/preflight", {})
            self.assertEqual(res.status, 200, res.text)
            data = res.json
            self.assertTrue(data["ok"])
            self.assertFalse(data["passed"], "全不可达时不该判定为通过")
            self.assertGreaterEqual(data["failures"], 1)
            names = [c["name"] for c in data["result"]["checks"]]
            self.assertIn("Telegram 连通", names)
            for check in data["result"]["checks"]:
                if check["status"] in ("fail", "warn"):
                    self.assertTrue(check["fix"], f"{check['name']} 缺 fix 提示")

    async def test_gaps_last_empty(self):
        with temp_dir() as root:
            await self._start(root)
            res = await request(self._port, "GET", "/api/gaps/last")
            self.assertEqual(res.status, 200)
            self.assertIsNone(res.json["result"])

    async def test_apply_settings_refreshes_every_component(self):
        """改配置后，所有持有客户端引用的组件都必须指向新对象。

        这是热重载最容易出错的地方：漏掉一个组件，它就会拿着已关闭的旧客户端，
        之后所有请求都失败（而且很难查）。
        """
        with temp_dir() as root:
            hub = await self._start(root)
            os.environ.pop("RMH_EMBY_URL", None)
            os.environ.pop("RMH_TMDB_API_KEY", None)

            old_library = hub.library
            old_tmdb = hub.tmdb
            old_http = hub.http
            old_scanner = hub.scanner
            old_reconciler = hub.reconciler
            old_notifier = hub.notifier
            old_gapfinder = hub.gapfinder

            from app.config import apply_overrides

            apply_overrides(
                cfg_dir := root / "config",
                changes={"library": {"url": "http://new-emby.local:8096"}, "tmdb": {"api_key": "new-key"}},
            )
            await hub.apply_settings(load_settings(cfg_dir, root / "state"))

            # 客户端对象本身必须换新
            self.assertIsNot(hub.library, old_library)
            self.assertIsNot(hub.tmdb, old_tmdb)
            self.assertIsNot(hub.http, old_http)
            self.assertEqual(hub.library.url, "http://new-emby.local:8096")
            self.assertEqual(hub.tmdb.api_key, "new-key")

            # 依赖它们的组件也要换新，且指向新客户端
            self.assertIsNot(hub.scanner, old_scanner)
            self.assertIsNot(hub.reconciler, old_reconciler)
            self.assertIsNot(hub.notifier, old_notifier)
            self.assertIsNot(hub.gapfinder, old_gapfinder)
            self.assertIs(hub.scanner.library, hub.library)
            self.assertIs(hub.scanner.tmdb, hub.tmdb)
            self.assertIs(hub.reconciler.library, hub.library)
            self.assertIs(hub.reconciler.tmdb, hub.tmdb)
            self.assertIs(hub.notifier.library, hub.library)
            self.assertIs(hub.gapfinder.http, hub.http)
            self.assertIs(hub.gapfinder.settings, hub.settings)

            # 订阅计划要保留（不能因为换配置就把轮询计划清空）
            self.assertIn("demo", hub.runtimes)

    async def test_feeds_check_endpoint(self):
        """一键体检所有 RSS 源：不需要 Emby/TMDB 也能用。"""
        from app import feedcheck as fc
        from app.rss import FeedItem

        with temp_dir() as root:
            hub = await self._start(
                root,
                subs_yaml=(
                    "subscriptions:\n"
                    "  - id: good\n"
                    "    name: 好源\n"
                    "    mode: feed\n"
                    "    rss: https://pt.example/good?passkey=SECRET\n"
                    "  - id: bad\n"
                    "    name: 坏源\n"
                    "    mode: feed\n"
                    "    rss: https://pt.example/bad?passkey=SECRET\n"
                    "  - id: off\n"
                    "    name: 停用的\n"
                    "    mode: feed\n"
                    "    rss: https://pt.example/off\n"
                    "    enabled: false\n"
                ),
            )

            async def handler(client: Any, url: str, **kwargs: Any) -> list[FeedItem]:
                if "bad" in url:
                    raise RuntimeError("HTTP 401 Unauthorized")
                return [
                    FeedItem(
                        title="某剧 S01E05 2160p HEVC",
                        link="https://pt.example/d/5",
                        download_url="https://pt.example/download.php?id=5",
                        guid="g5",
                        episode=5,
                        size_bytes=12_000_000_000,
                    )
                ]

            original = fc.fetch_feed
            fc.fetch_feed = handler
            try:
                res = await request(self._port, "POST", "/api/feeds/check", {})
            finally:
                fc.fetch_feed = original

            self.assertEqual(res.status, 200, res.text)
            s = res.json["summary"]
            self.assertEqual(s["total"], 2, "停用的源不该被检查")
            self.assertEqual(s["ok"], 1)
            self.assertEqual(s["failed"], 1)
            self.assertEqual(s["skipped_disabled"], 1)

            raw = res.text
            self.assertNotIn("passkey=SECRET", raw, "URL 里的密钥必须打码")
            checks = {c["sub_name"]: c for c in res.json["result"]["checks"]}
            self.assertTrue(checks["好源"]["ok"])
            self.assertEqual(checks["好源"]["item_count"], 1)
            self.assertIn("2160p", checks["好源"]["previews"][0]["badges"])
            self.assertFalse(checks["坏源"]["ok"])
            self.assertIn("passkey", checks["坏源"]["error"])

            res = await request(self._port, "GET", "/api/feeds/last")
            self.assertEqual(res.status, 200)
            self.assertIsNotNone(res.json["result"])

    async def test_feeds_check_requires_subscriptions(self):
        with temp_dir() as root:
            hub = await self._start(root)
            hub.settings.subscriptions = []
            res = await request(self._port, "POST", "/api/feeds/check", {})
            self.assertEqual(res.status, 400)
            self.assertIn("订阅", res.json["error"])

    async def test_check_endpoint_reports_progress(self):
        with temp_dir() as root:
            hub = await self._start(root)
            sub = next(iter(hub.runtimes.values())).sub

            async def fake_series(*args: Any, **kwargs: Any):
                from app.tmdb import EpisodeInfo, SeasonInfo, SeriesInfo

                return SeriesInfo(
                    tmdb_id=1396, name="示例剧", year=2020, total_seasons=1, total_episodes=3,
                    seasons=[SeasonInfo(number=1, episode_count=3, episodes=[
                        EpisodeInfo(season=1, episode=i, air_date=None) for i in (1, 2, 3)
                    ])],
                )

            async def fake_find(name: str, **kwargs: Any):
                from app.emby import LocalEpisode, LocalSeries

                local = LocalSeries(item_id="s1", name="示例剧", provider_ids={"Tmdb": "1396"})
                local.episodes = [LocalEpisode(season=1, episode=1), LocalEpisode(season=1, episode=2)]
                return local

            hub.tmdb.series = fake_series  # type: ignore[assignment]
            hub.library.find_series = fake_find  # type: ignore[assignment]

            res = await request(self._port, "POST", "/api/check", {"id": sub.id})
            self.assertEqual(res.status, 200, res.text)
            row = res.json["results"][0]
            self.assertTrue(row["ok"])
            self.assertEqual((row["owned"], row["total"]), (2, 3))
            self.assertEqual(row["missing"], "S01E03")
            self.assertFalse(row["done"])

            # 指定不存在的订阅要报 400
            res = await request(self._port, "POST", "/api/check", {"id": "ghost"})
            self.assertEqual(res.status, 400)

    async def test_scan_last_empty_then_value(self):
        with temp_dir() as root:
            await self._start(root)
            res = await request(self._port, "GET", "/api/scan/last")
            self.assertEqual(res.status, 200)
            self.assertIsNone(res.json["result"])

            # 塞一个假的扫描结果进去
            from app.libraryscan import STATUS_PARTIAL, ScanResult, SeriesScan

            result = ScanResult(library_total=2, scanned=2)
            result.series = [
                SeriesScan("a", "缺集剧", tmdb_id=1, total=10, aired=10, owned=8, status=STATUS_PARTIAL,
                           missing_codes=["S01E09", "S01E10"]),
            ]
            result.finished_at = result.started_at + 1
            self._ui.last_scan = result

            res = await request(self._port, "GET", "/api/scan/last")
            data = res.json
            self.assertEqual(data["summary"]["partial"], 1)
            self.assertEqual(data["summary"]["missing_episodes"], 2)
            self.assertEqual(data["result"]["series"][0]["name"], "缺集剧")

    async def test_unknown_path_and_method(self):
        with temp_dir() as root:
            await self._start(root)
            res = await request(self._port, "GET", "/api/nope")
            self.assertEqual(res.status, 404)
            self.assertIn("未知路径", res.json["error"])

            res = await request(self._port, "PUT", "/api/config")
            self.assertEqual(res.status, 405)

    async def test_index_page_served(self):
        with temp_dir() as root:
            await self._start(root)
            res = await request(self._port, "GET", "/")
            self.assertEqual(res.status, 200)
            self.assertIn("text/html", res.headers["content-type"])
            self.assertIn("RSS Media Hub", res.text)
            # 单页界面必须自带脚本，不能依赖外部 CDN
            self.assertIn("<script>", res.text)
            self.assertNotIn("http://", res.text.replace("http://127.0.0.1", "").replace("http://192", ""))

    async def test_bad_json_body(self):
        with temp_dir() as root:
            await self._start(root)
            import asyncio

            reader, writer = await asyncio.open_connection("127.0.0.1", self._port)
            try:
                payload = b"{not json"
                writer.write(
                    b"POST /api/config HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: "
                    + str(len(payload)).encode()
                    + b"\r\nConnection: close\r\n\r\n"
                    + payload
                )
                await writer.drain()
                data = await asyncio.wait_for(reader.read(), timeout=10)
            finally:
                with contextlib.suppress(Exception):
                    writer.close()
            self.assertIn(b"400", data.split(b"\r\n")[0])
            self.assertIn(b"JSON", data)


if __name__ == "__main__":
    unittest.main()
