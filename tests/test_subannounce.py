"""「已添加订阅」TG 确认通知的测试。

覆盖三块：
  1. render_sub_added 的文案拼装（年/季/HTML 转义）
  2. Hub.announce_subscription 的取海报分支：
     show 模式直取 TMDB / feed 模式精确命中才配图 / 拿不到就纯文字
  3. 网页 UI / CLI 添加订阅 → 真的触发了通知
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import os
import shutil
import unittest
from pathlib import Path
from typing import Any

from app.config import Subscription, load_settings
from app.main import Hub, cmd_add
from app.notify import Notifier
from app.telegram import TgResult
from app.webserve import WebUI

_counter = itertools.count()

SHOW_RAW = {
    "id": 777,
    "name": "风华令",
    "original_name": "Feng Hua Ling",
    "first_air_date": "2026-05-01",
    "poster_path": "/fhl.jpg",
}


@contextlib.contextmanager
def temp_dir():
    base = Path(os.environ.get("RMH_TEST_TMP") or Path(__file__).resolve().parent / ".tmp")
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"announce-{os.getpid()}-{next(_counter)}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def make_settings(root: Path) -> Settings:
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "subscriptions.yaml").write_text("subscriptions: []\n", encoding="utf-8")
    return load_settings(config_dir, root / "state")


class StubTg:
    """记录调用的假 TelegramSender。"""

    def __init__(self) -> None:
        self.bot_token = "111:FAKE"
        self.chat_id = "-100123"
        self.photos: list[tuple[bytes, str]] = []
        self.messages: list[str] = []
        self.photo_ok = True

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    async def aclose(self) -> None:
        pass

    async def send_photo(self, photo: bytes, caption: str = "") -> TgResult:
        self.photos.append((photo, caption))
        return TgResult(self.photo_ok, message_id=len(self.photos))

    async def send_message(self, text: str, **kwargs: Any) -> TgResult:
        self.messages.append(text)
        return TgResult(True, message_id=len(self.messages))


class StubPoster:
    def __init__(self) -> None:
        self.asked: list[str] = []
        self.data: bytes | None = b"FAKE-PNG"

    async def aclose(self) -> None:
        pass

    async def download_poster(self, poster_path: str) -> bytes | None:
        self.asked.append(poster_path)
        return self.data


class StubTmdb:
    def __init__(self) -> None:
        self.raw_by_id: dict[int, dict[str, Any]] = {}
        self.search_results: list[dict[str, Any]] = []
        self.fail = False
        self.resolve_calls = 0
        self.search_calls = 0

    async def aclose(self) -> None:
        pass

    async def resolve(
        self, name: str | None, tmdb_id: int | None = None, year: int | None = None
    ) -> dict[str, Any]:
        self.resolve_calls += 1
        if self.fail:
            raise RuntimeError("tmdb down")
        if tmdb_id in self.raw_by_id:
            return self.raw_by_id[tmdb_id]
        from app.tmdb import TmdbNotFound

        raise TmdbNotFound(f"no tv {tmdb_id}")

    async def search_tv(self, name: str, year: int | None = None) -> list[dict[str, Any]]:
        self.search_calls += 1
        return self.search_results


def build_hub(root: Path) -> tuple[Hub, StubTg, StubTmdb, StubPoster]:
    settings = make_settings(root)
    settings.tmdb.api_key = "fake-key"
    hub = Hub(settings)
    tg, tmdb, poster = StubTg(), StubTmdb(), StubPoster()
    hub.tg = tg  # type: ignore[assignment]
    hub.tmdb = tmdb  # type: ignore[assignment]
    hub.poster_resolver = poster  # type: ignore[assignment]
    return hub, tg, tmdb, poster


class RenderSubAddedTest(unittest.TestCase):
    def setUp(self) -> None:
        self._stack = contextlib.ExitStack()
        root = self._stack.enter_context(temp_dir())
        self.n = Notifier(make_settings(root), None, None)  # type: ignore[arg-type]

    def tearDown(self) -> None:
        self._stack.close()

    def test_full(self):
        sub = Subscription(id="fhl", name="label", season=1)
        self.assertEqual(
            self.n.render_sub_added(sub, title="风华令", year=2026),
            "风华令 (2026) S01 已添加订阅",
        )

    def test_no_year_no_season(self):
        sub = Subscription(id="x", name="风华令")
        self.assertEqual(self.n.render_sub_added(sub), "风华令 已添加订阅")

    def test_only_season(self):
        sub = Subscription(id="x", name="风华令", season=2)
        self.assertEqual(self.n.render_sub_added(sub, title="风华令"), "风华令 S02 已添加订阅")

    def test_escapes_html(self):
        sub = Subscription(id="x", name="<b>AT&T</b>")
        text = self.n.render_sub_added(sub)
        self.assertNotIn("<b>", text)
        self.assertIn("&lt;b&gt;AT&amp;T&lt;/b&gt; 已添加订阅", text)


class AnnounceTest(unittest.IsolatedAsyncioTestCase):
    async def test_show_mode_sends_photo_with_caption(self):
        with temp_dir() as root:
            hub, tg, tmdb, poster = build_hub(root)
            try:
                tmdb.raw_by_id[777] = dict(SHOW_RAW)
                sub = Subscription(id="fhl", name="风华令", tmdb_id=777, season=1)
                await hub.announce_subscription(sub)
                self.assertEqual(len(tg.photos), 1, "应该发一张海报")
                self.assertEqual(tg.photos[0][0], b"FAKE-PNG")
                self.assertEqual(tg.photos[0][1], "风华令 (2026) S01 已添加订阅")
                self.assertEqual(tg.messages, [], "海报成功就不该再发文字")
            finally:
                await hub.aclose()

    async def test_feed_exact_match_gets_poster(self):
        with temp_dir() as root:
            hub, tg, tmdb, poster = build_hub(root)
            try:
                tmdb.search_results = [dict(SHOW_RAW)]
                sub = Subscription(id="fhl", name="风华令")  # feed 模式，无 tmdb_id
                await hub.announce_subscription(sub)
                self.assertEqual(len(tg.photos), 1)
                self.assertEqual(tg.photos[0][1], "风华令 (2026) 已添加订阅")
            finally:
                await hub.aclose()

    async def test_feed_fuzzy_miss_text_only(self):
        """名字只是个标签（如站点名），TMDB 只有像的结果 → 不配图，发文字。"""
        with temp_dir() as root:
            hub, tg, tmdb, poster = build_hub(root)
            try:
                tmdb.search_results = [
                    {"id": 1, "name": "风华令传奇", "first_air_date": "2020-01-01", "poster_path": "/x.jpg"}
                ]
                sub = Subscription(id="ub", name="UB")
                await hub.announce_subscription(sub)
                self.assertEqual(tg.photos, [], "模糊命中不该配海报")
                self.assertEqual(tg.messages, ["UB 已添加订阅"])
            finally:
                await hub.aclose()

    async def test_tmdb_failure_still_sends_text(self):
        with temp_dir() as root:
            hub, tg, tmdb, poster = build_hub(root)
            try:
                tmdb.fail = True
                sub = Subscription(id="fhl", name="风华令", tmdb_id=777, season=1)
                await hub.announce_subscription(sub)
                self.assertEqual(tg.photos, [])
                self.assertEqual(tg.messages, ["风华令 S01 已添加订阅"])
            finally:
                await hub.aclose()

    async def test_poster_download_fails_falls_back_to_text(self):
        with temp_dir() as root:
            hub, tg, tmdb, poster = build_hub(root)
            try:
                tmdb.raw_by_id[777] = dict(SHOW_RAW)
                poster.data = None
                sub = Subscription(id="fhl", name="风华令", tmdb_id=777)
                await hub.announce_subscription(sub)
                self.assertEqual(tg.photos, [])
                self.assertEqual(tg.messages, ["风华令 (2026) 已添加订阅"])
            finally:
                await hub.aclose()

    async def test_send_photo_fails_falls_back_to_text(self):
        with temp_dir() as root:
            hub, tg, tmdb, poster = build_hub(root)
            try:
                tmdb.raw_by_id[777] = dict(SHOW_RAW)
                tg.photo_ok = False
                sub = Subscription(id="fhl", name="风华令", tmdb_id=777)
                await hub.announce_subscription(sub)
                self.assertEqual(len(tg.photos), 1)
                self.assertEqual(tg.messages, ["风华令 (2026) 已添加订阅"], "图片失败要补发文字")
            finally:
                await hub.aclose()

    async def test_telegram_disabled_is_silent(self):
        with temp_dir() as root:
            hub, tg, tmdb, poster = build_hub(root)
            try:
                tg.bot_token = ""
                sub = Subscription(id="fhl", name="风华令", tmdb_id=777)
                await hub.announce_subscription(sub)  # 不应抛异常
                self.assertEqual(tg.photos, [])
                self.assertEqual(tmdb.resolve_calls, 0, "TG 都没配就不该去查 TMDB")
            finally:
                await hub.aclose()

    async def test_tmdb_disabled_text_only(self):
        with temp_dir() as root:
            hub, tg, tmdb, poster = build_hub(root)
            try:
                hub.settings.tmdb.api_key = ""
                sub = Subscription(id="fhl", name="风华令", tmdb_id=777)
                await hub.announce_subscription(sub)
                self.assertEqual(tmdb.resolve_calls, 0)
                self.assertEqual(tg.messages, ["风华令 已添加订阅"])
            finally:
                await hub.aclose()


class WebUiAddTriggersAnnounceTest(unittest.IsolatedAsyncioTestCase):
    def _body(self, **extra: Any) -> bytes:
        payload: dict[str, Any] = {
            "subscription": {
                "id": "fhl",
                "name": "风华令",
                "tmdb_id": 777,
                "season": 1,
                "rss": "https://pt.example/rss?passkey=SECRET",
            }
        }
        payload.update(extra)
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    async def test_post_new_subscription_announces(self):
        with temp_dir() as root:
            hub, tg, tmdb, poster = build_hub(root)
            try:
                tmdb.raw_by_id[777] = dict(SHOW_RAW)
                ui = WebUI(hub)
                resp = await ui.h_subs_post(headers={}, query={}, body=self._body())
                self.assertTrue(resp["ok"], resp)
                await asyncio.gather(*hub._bg)  # 等后台通知任务跑完
                self.assertEqual(len(tg.photos), 1, "网页加订阅应该触发 TG 通知")
                self.assertEqual(tg.photos[0][1], "风华令 (2026) S01 已添加订阅")

                # overwrite 已有订阅 → 不再重复通知
                tg.photos.clear()
                resp = await ui.h_subs_post(headers={}, query={}, body=self._body(overwrite=True))
                self.assertTrue(resp["ok"])
                await asyncio.gather(*hub._bg)
                self.assertEqual(tg.photos, [], "覆盖已有订阅不该再发通知")
            finally:
                await hub.aclose()

    async def test_cli_add_announces(self):
        with temp_dir() as root:
            hub, tg, tmdb, poster = build_hub(root)
            tmdb.raw_by_id[777] = dict(SHOW_RAW)
            await hub.aclose()  # cmd_add 自己会建 Hub；这里只用它的 settings
            settings = make_settings(root)
            settings.tmdb.api_key = "fake-key"

            # 让 cmd_add 内部新建的 Hub 用上桩（和 test_integration._patch_hub 同思路）
            from app import main as main_mod

            created: list[Hub] = []

            def fake_hub_ctor(s: Any) -> Hub:
                h = Hub(s)
                h.tg = tg  # type: ignore[assignment]
                h.tmdb = tmdb  # type: ignore[assignment]
                h.poster_resolver = poster  # type: ignore[assignment]
                created.append(h)
                return h

            original = main_mod.Hub
            main_mod.Hub = fake_hub_ctor  # type: ignore[assignment]
            try:
                code = await cmd_add(settings, name="风华令", tmdb_id=777, season=1)
            finally:
                main_mod.Hub = original  # type: ignore[assignment]
            self.assertEqual(code, 0)
            self.assertEqual(len(tg.photos), 1)
            self.assertEqual(tg.photos[0][1], "风华令 (2026) S01 已添加订阅")
            for h in created:
                await h.aclose()


if __name__ == "__main__":
    unittest.main()
