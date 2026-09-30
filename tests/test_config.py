"""配置解析测试：YAML 读写、订阅规则、环境变量优先级。"""

from __future__ import annotations

import contextlib
import itertools
import os
import shutil
import unittest
from pathlib import Path

from app.config import (
    MODE_FEED,
    MODE_SHOW,
    ConfigError,
    Subscription,
    load_subscriptions,
    load_yaml_text,
    parse_subscription,
    save_subscriptions,
)

_counter = itertools.count()


@contextlib.contextmanager
def temp_dir():
    """比 TemporaryDirectory 更宽容：在工作区里建临时目录，清理失败不影响断言。

    不用 tempfile.mkdtemp：它在 Windows 上会把目录权限设成 0700，
    在受限沙箱里会导致后续写入被拒。
    """
    base = Path(os.environ.get("RMH_TEST_TMP") or Path(__file__).resolve().parent / ".tmp")
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"case-{os.getpid()}-{next(_counter)}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class YamlTest(unittest.TestCase):
    def test_nested_structures(self):
        text = """
# 注释
telegram:
  bot_token: "123:abc"
  send_poster: true
  thread_id: 42
library:
  url: http://emby:8096
  quality:
    - 1080p
    - 2160p
runtime:
  poll_interval: 600
"""
        data = load_yaml_text(text)
        self.assertEqual(data["telegram"]["bot_token"], "123:abc")
        self.assertIs(data["telegram"]["send_poster"], True)
        self.assertEqual(data["telegram"]["thread_id"], 42)
        self.assertEqual(data["library"]["quality"], ["1080p", "2160p"])
        self.assertEqual(data["runtime"]["poll_interval"], 600)

    def test_inline_list_and_comment_inside_string(self):
        data = load_yaml_text('quality: ["1080p", "720p"]  # 画质\nname: "A # B"\n')
        self.assertEqual(data["quality"], ["1080p", "720p"])
        self.assertEqual(data["name"], "A # B")

    def test_list_of_dicts_roundtrip(self):
        text = """
subscriptions:
  - id: a
    name: Show A
    tmdb_id: 100
    rss: https://x/rss?passkey=k
    exclude_filter: "预告|花絮"
"""
        data = load_yaml_text(text)
        self.assertEqual(len(data["subscriptions"]), 1)
        self.assertEqual(data["subscriptions"][0]["tmdb_id"], 100)


class SubscriptionTest(unittest.TestCase):
    def test_parse_basic(self):
        sub = parse_subscription({"name": "Show", "rss": "https://x/rss", "tmdb_id": 5, "year": 2024})
        self.assertEqual(sub.id, "show")
        self.assertEqual(sub.tmdb_id, 5)
        self.assertTrue(sub.remove_when_done)

    def test_rss_urls_split(self):
        sub = parse_subscription({"name": "S", "rss": "https://a/rss, https://b/rss https://c/rss"})
        self.assertEqual(sub.rss_urls, ["https://a/rss", "https://b/rss", "https://c/rss"])

    def test_filters(self):
        sub = Subscription(id="s", name="S", name_filter="1080p", exclude_filter="预告", quality=["1080p"])
        ok, _ = sub.allows_title("Show S01E01 1080p")
        self.assertTrue(ok)

        # 命中 exclude_filter
        ok, reason = sub.allows_title("Show S01E01 1080p 预告")
        self.assertFalse(ok)
        self.assertIn("exclude", reason)

        # 只允许 2160p 时，1080p 版本应被画质白名单挡掉
        hd = Subscription(id="hd", name="HD", quality=["2160p"])
        ok, reason = hd.allows_title("Show S01E01 1080p")
        self.assertFalse(ok)
        self.assertIn("画质", reason)
        ok, _ = hd.allows_title("Show S01E01 2160p")
        self.assertTrue(ok)

        # 不匹配 name_filter
        ok, reason = sub.allows_title("Show S01E01 2160p")
        self.assertFalse(ok)
        self.assertIn("name_filter", reason)

    def test_missing_everything_raises(self):
        with self.assertRaises(ConfigError):
            parse_subscription({})

    def test_save_and_reload(self):
        subs = [
            Subscription(id="a", name="Show A", tmdb_id=1, rss="https://x/rss", quality=["1080p"]),
            Subscription(id="b", name="Show B", season=2, remove_when_done=False),
        ]
        with temp_dir() as tmp:
            path = Path(tmp) / "subscriptions.yaml"
            save_subscriptions(path, subs)
            loaded = load_subscriptions(path)
        self.assertEqual([s.id for s in loaded], ["a", "b"])
        self.assertEqual(loaded[0].quality, ["1080p"])
        self.assertEqual(loaded[1].season, 2)
        self.assertFalse(loaded[1].remove_when_done)

    def test_empty_file(self):
        with temp_dir() as tmp:
            path = Path(tmp) / "nope.yaml"
            self.assertEqual(load_subscriptions(path), [])


class SubscriptionModeTest(unittest.TestCase):
    """feed（订阅源全量）与 show（按剧追踪）两种模式。"""

    def test_default_mode_inferred_from_tmdb_id(self):
        # 填了 tmdb_id → 按剧追踪
        self.assertEqual(parse_subscription({"name": "A", "tmdb_id": 1}).mode, MODE_SHOW)
        # 没填 → 订阅源全量
        self.assertEqual(parse_subscription({"name": "A", "rss": "https://x/rss"}).mode, MODE_FEED)

    def test_explicit_mode_wins(self):
        # 显式写了 mode 就以它为准，即使填了 tmdb_id 也可以走 feed
        self.assertEqual(
            parse_subscription({"name": "A", "tmdb_id": 1, "mode": "feed", "rss": "https://x/rss"}).mode,
            MODE_FEED,
        )
        self.assertEqual(parse_subscription({"name": "A", "mode": "show", "rss": "https://x/rss"}).mode, MODE_SHOW)

    def test_aliases_accepted(self):
        for alias in ("all", "rss", "raw", "forward", "full", "FEED", " Feed "):
            sub = parse_subscription({"name": "A", "rss": "https://x/rss", "mode": alias})
            self.assertEqual(sub.mode, MODE_FEED, f"{alias!r} 应该被识别为 feed")

    def test_invalid_mode_rejected(self):
        with self.assertRaises(ConfigError):
            parse_subscription({"name": "A", "rss": "https://x/rss", "mode": "banana"})

    def test_seed_default_depends_on_mode(self):
        # feed 默认把历史条目也补推（用户要的就是"全量"）
        self.assertFalse(parse_subscription({"name": "A", "rss": "https://x/rss"}).seed)
        # show 默认静默登记，避免一上线被历史刷屏
        self.assertTrue(parse_subscription({"name": "A", "tmdb_id": 1}).seed)

    def test_seed_can_be_overridden(self):
        self.assertTrue(parse_subscription({"name": "A", "rss": "https://x/rss", "seed": True}).seed)
        self.assertFalse(parse_subscription({"name": "A", "tmdb_id": 1, "seed": False}).seed)
        # YAML 里的 "true"/"false" 字符串也要能吃
        self.assertFalse(parse_subscription({"name": "A", "rss": "https://x/rss", "seed": "false"}).seed)

    def test_show_mode_allows_name_only(self):
        """show 模式允许只给剧名，靠名称去 TMDB 搜索匹配（不强制 tmdb_id）。"""
        sub = parse_subscription({"name": "只有名字的剧", "rss": "https://x/rss", "mode": "show"})
        self.assertTrue(sub.is_show)
        self.assertIsNone(sub.tmdb_id)
        sub.validate()

    def test_feed_mode_with_tmdb_needs_rss(self):
        """feed 模式本身跟 tmdb_id 没关系，填了却不给 rss 才算配错。"""
        with self.assertRaises(ConfigError):
            parse_subscription({"name": "A", "mode": "feed", "tmdb_id": 1})

    def test_draft_subscription_without_rss_is_allowed(self):
        """允许先建订阅后补 RSS（网页表单是逐个字段填的）。"""
        sub = parse_subscription({"name": "还没填RSS", "tmdb_id": 5})
        self.assertTrue(sub.is_show)
        self.assertEqual(sub.rss_urls, [])
        sub.validate()

    def test_feed_mode_needs_no_tmdb(self):
        sub = parse_subscription({"name": "整站播报", "rss": "https://x/rss"})
        self.assertTrue(sub.is_feed)
        self.assertIsNone(sub.tmdb_id)
        self.assertEqual(sub.mode_label, "订阅源全量")
        sub.validate()  # 不应该抛错

    def test_str_shorthand_is_feed(self):
        """subscriptions.yaml 里直接写一行 RSS 地址也应可用，且是 feed 模式。"""
        sub = parse_subscription("https://x/rss?passkey=k")
        self.assertTrue(sub.is_feed)
        self.assertEqual(sub.rss, "https://x/rss?passkey=k")

    def test_mode_and_seed_roundtrip(self):
        subs = [
            Subscription(id="f", name="全量", rss="https://x/rss", mode=MODE_FEED),
            Subscription(id="s", name="追剧", rss="https://x/rss", tmdb_id=5, mode=MODE_SHOW, seed=False),
        ]
        with temp_dir() as tmp:
            path = tmp / "subscriptions.yaml"
            save_subscriptions(path, subs)
            text = path.read_text(encoding="utf-8")
            loaded = load_subscriptions(path)
        # feed 是默认值，不写出来；但读回来必须还是 feed
        self.assertNotIn("mode: feed", text)
        self.assertEqual([s.mode for s in loaded], [MODE_FEED, MODE_SHOW])
        # 显式写了 seed=False 就保留
        self.assertFalse(loaded[1].seed)
        self.assertIn("mode: show", text)

    def test_mode_survives_multiple_reloads(self):
        """反复保存/读取不能把 mode 弄丢（否则订阅行为会悄悄变）。"""
        with temp_dir() as tmp:
            path = tmp / "subscriptions.yaml"
            subs = [Subscription(id="f", name="全量", rss="https://x/rss", mode=MODE_FEED)]
            for _ in range(3):
                save_subscriptions(path, subs)
                subs = load_subscriptions(path)
                self.assertEqual(subs[0].mode, MODE_FEED)


class EnvPriorityTest(unittest.TestCase):
    def test_env_overrides_file(self):
        from app.config import load_settings

        with temp_dir() as tmp:
            cfg = Path(tmp) / "config"
            state = Path(tmp) / "state"
            cfg.mkdir()
            (cfg / "config.yaml").write_text(
                "telegram:\n  bot_token: file-token\ntmdb:\n  api_key: file-key\n", encoding="utf-8"
            )
            (cfg / "subscriptions.yaml").write_text("subscriptions:\n  - name: X\n    tmdb_id: 9\n", encoding="utf-8")
            os.environ["RMH_TG_BOT_TOKEN"] = "env-token"
            os.environ["RMH_TMDB_API_KEY"] = "env-key"
            os.environ["RMH_POLL_INTERVAL"] = "123"
            try:
                settings = load_settings(cfg, state)
            finally:
                for key in ("RMH_TG_BOT_TOKEN", "RMH_TMDB_API_KEY", "RMH_POLL_INTERVAL"):
                    os.environ.pop(key, None)
        self.assertEqual(settings.telegram.bot_token, "env-token")
        self.assertEqual(settings.tmdb.api_key, "env-key")
        self.assertEqual(settings.poll_interval, 123)
        self.assertEqual(len(settings.subscriptions), 1)
        self.assertEqual(settings.subscriptions[0].tmdb_id, 9)


if __name__ == "__main__":
    unittest.main()
