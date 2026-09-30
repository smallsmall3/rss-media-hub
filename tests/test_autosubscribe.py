"""自动订阅测试：feed 源匹配到 TMDB 剧集 → 转 show 订阅 + 删原 feed 源。"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import os
import shutil
import unittest
from pathlib import Path
from typing import Any

from app.config import Subscription, load_settings
from app.main import Hub
from app.notify import ItemView

_counter = itertools.count()


@contextlib.contextmanager
def temp_dir():
    base = Path(os.environ.get("RMH_TEST_TMP") or Path(__file__).resolve().parent / ".tmp")
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"autosub-{os.getpid()}-{next(_counter)}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def make_hub(root: Path) -> Hub:
    cfg = root / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "subscriptions.yaml").write_text(
        "subscriptions:\n"
        "  - id: my-feed\n"
        "    name: 单剧追更源\n"
        "    rss: https://pt.example/rss?passkey=SECRET\n"
        "    mode: feed\n",
        encoding="utf-8",
    )
    settings = load_settings(cfg, root / "state")
    settings.tmdb.api_key = "fake-key"
    return Hub(settings)


def view(title: str, episode: str = "") -> ItemView:
    from app.release import classify_item, describe_release

    tags = describe_release(title)
    kind, icon = classify_item(title, has_episode=bool(episode))
    return ItemView(title=title, episode_label=episode, kind=kind, icon=icon,
                    badges=tags.badges(), rank=tags.quality_rank())


class AutoSubscribeTest(unittest.IsolatedAsyncioTestCase):
    async def test_episode_feed_auto_subscribes_and_removes_feed(self):
        with temp_dir() as root:
            hub = make_hub(root)
            try:
                hub.settings.auto_subscribe = True

                # 打桩 poster_resolver.resolve_detail → 返回命中剧集
                class Hit:
                    def __init__(self):
                        self.data = b"\xff\xd8x"
                        self.name = "佳期如梦"
                        self.tmdb_id = 48235
                        self.year = 2010

                calls = []

                async def fake_resolve_detail(title):
                    calls.append(title)
                    return Hit()

                hub.poster_resolver.resolve_detail = fake_resolve_detail  # type: ignore[assignment]

                # 打桩 tmdb.resolve（subscribe_from_tmdb 内部用）
                async def fake_resolve(name, tmdb_id, year=None):
                    return {"id": tmdb_id, "name": "佳期如梦", "first_air_date": "2010-06-04"}

                hub.tmdb.resolve = fake_resolve  # type: ignore[assignment]

                # 打桩 remove_subscription（避免真的写文件+db）
                removed: list[str] = []

                async def fake_remove(sub, reason=""):
                    removed.append(sub.id)

                hub.remove_subscription = fake_remove  # type: ignore[assignment]

                sub = hub.settings.subscriptions[0]
                result = await hub.auto_subscribe_feed(
                    sub, [view("佳期如梦 S01E06 2160p WEB-DL", "S01E06")]
                )

                self.assertIsNotNone(result, "应转成 show 订阅")
                self.assertEqual(result.tmdb_id, 48235)
                self.assertEqual(result.name, "佳期如梦")
                self.assertEqual(result.rss, "https://pt.example/rss?passkey=SECRET", "沿用同站 RSS")
                self.assertEqual(removed, ["my-feed"], "原 feed 源应被删除")
            finally:
                await hub.aclose()

    async def test_movie_feed_not_auto_subscribed(self):
        """电影（非连续剧集）不自动订阅。"""
        with temp_dir() as root:
            hub = make_hub(root)
            try:
                hub.settings.auto_subscribe = True

                async def fake_resolve_detail(title):
                    raise AssertionError("电影不该去查 TMDB")

                hub.poster_resolver.resolve_detail = fake_resolve_detail  # type: ignore[assignment]

                sub = hub.settings.subscriptions[0]
                # 电影标题：无集号，classify_item 判为电影
                result = await hub.auto_subscribe_feed(
                    sub, [view("某电影 2024 2160p BluRay")]
                )
                self.assertIsNone(result, "电影不自动订阅")
            finally:
                await hub.aclose()

    async def test_disabled_does_nothing(self):
        with temp_dir() as root:
            hub = make_hub(root)
            try:
                hub.settings.auto_subscribe = False  # 默认关
                sub = hub.settings.subscriptions[0]
                result = await hub.auto_subscribe_feed(
                    sub, [view("某剧 S01E01 1080p", "S01E01")]
                )
                self.assertIsNone(result, "关闭自动订阅时什么都不做")
            finally:
                await hub.aclose()

    async def test_no_tmdb_match_does_nothing(self):
        with temp_dir() as root:
            hub = make_hub(root)
            try:
                hub.settings.auto_subscribe = True

                async def fake_resolve_detail(title):
                    return None  # 没匹配到 TMDB

                hub.poster_resolver.resolve_detail = fake_resolve_detail  # type: ignore[assignment]

                sub = hub.settings.subscriptions[0]
                result = await hub.auto_subscribe_feed(
                    sub, [view("某剧 S01E01 1080p", "S01E01")]
                )
                self.assertIsNone(result, "没匹配到 TMDB 就不转")
            finally:
                await hub.aclose()


if __name__ == "__main__":
    unittest.main()
