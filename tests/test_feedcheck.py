"""订阅源体检（feedcheck）测试。

体检的价值在于"把失败原因翻译成该怎么办"，所以错误翻译也要测。
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from app import feedcheck as fc
from app.config import Subscription
from app.feedcheck import FeedsReport, build_preview, check_feeds, collect_sources
from app.rss import FeedItem


def item(title: str, *, episode=None, size=1_500_000_000, published=None, url="", link="") -> FeedItem:
    return FeedItem(
        title=title,
        link=link or "https://pt.example/details/1",
        download_url=url,
        guid=title,
        size_bytes=size,
        published=published or datetime(2025, 2, 5, 12, 0, tzinfo=timezone.utc),
        episode=episode,
    )


class CollectSourcesTest(unittest.TestCase):
    def test_flattens_and_dedupes(self):
        subs = [
            Subscription(id="a", name="A", mode="feed", rss="https://pt/rss1"),
            Subscription(id="b", name="B", mode="feed", rss="https://pt/rss2 https://pt/rss1"),
            Subscription(id="c", name="C", mode="feed", rss="https://pt/rss3", enabled=False),
        ]
        sources, skipped = collect_sources(subs)
        self.assertEqual([u for _, u in sources], ["https://pt/rss1", "https://pt/rss2"])
        self.assertEqual(skipped, 1, "停用的订阅要被统计但不出现在列表里")

    def test_empty(self):
        sources, skipped = collect_sources([])
        self.assertEqual(sources, [])
        self.assertEqual(skipped, 0)


class PreviewTest(unittest.TestCase):
    def test_builds_badges_and_size(self):
        p = build_preview(item("某剧 S01E05 2160p HEVC", episode=5))
        self.assertIn("2160p", p.badges)
        self.assertIn("HEVC", p.badges)
        self.assertEqual(p.size, "1.40 GB")
        self.assertTrue(p.has_download is False)

    def test_truncates_long_title(self):
        p = build_preview(item("某" * 200))
        self.assertLessEqual(len(p.title), 90)
        self.assertTrue(p.title.endswith("…"))


class ErrorExplainTest(unittest.TestCase):
    def test_auth(self):
        msg = fc._explain_error(RuntimeError("HTTP 401 Unauthorized"))
        self.assertIn("passkey", msg)

    def test_not_found(self):
        self.assertIn("地址不存在", fc._explain_error(RuntimeError("404 Not Found")))

    def test_timeout(self):
        self.assertIn("超时", fc._explain_error(RuntimeError("ReadTimeout: timed out")))

    def test_dns(self):
        self.assertIn("域名解析", fc._explain_error(RuntimeError("[Errno 11001] getaddrinfo failed")))

    def test_unknown_passthrough(self):
        self.assertIn("奇怪的问题", fc._explain_error(RuntimeError("奇怪的问题")))


class CheckFeedsTest(unittest.IsolatedAsyncioTestCase):
    async def _run(self, handler, subs):
        settings = type("S", (), {"subscriptions": subs})()
        original = fc.fetch_feed
        fc.fetch_feed = handler
        try:
            return await check_feeds(settings, http=None)
        finally:
            fc.fetch_feed = original

    async def test_all_ok(self):
        subs = [
            Subscription(id="a", name="源A", mode="feed", rss="https://pt/rss1"),
            Subscription(id="b", name="源B", mode="feed", rss="https://pt/rss2"),
        ]

        async def handler(client, url, **kw):
            if url.endswith("rss1"):
                return [item("剧A S01E01", episode=1, url="https://pt/dl/1"), item("剧A S01E02", episode=2)]
            return [item("剧B S01E01 2160p HEVC", episode=1)]

        report = await self._run(handler, subs)
        self.assertEqual(len(report.checks), 2)
        self.assertEqual(report.ok_count, 2)
        self.assertTrue(report.healthy)
        self.assertEqual(report.checks[0].item_count, 2)
        self.assertEqual(len(report.checks[0].previews), 2)
        # 最新排前面
        self.assertEqual(report.checks[0].newest, "02-05 20:00")

    async def test_partial_failure_does_not_break_others(self):
        subs = [
            Subscription(id="a", name="好源", mode="feed", rss="https://pt/good"),
            Subscription(id="b", name="坏源", mode="feed", rss="https://pt/bad"),
        ]

        async def handler(client, url, **kw):
            if url.endswith("bad"):
                raise RuntimeError("HTTP 403 Forbidden")
            return [item("正常条目")]

        report = await self._run(handler, subs)
        self.assertEqual(report.ok_count, 1)
        self.assertEqual(len(report.failed), 1)
        self.assertFalse(report.healthy)
        bad = report.failed[0]
        self.assertEqual(bad.sub_name, "坏源")
        self.assertIn("passkey", bad.error)

    async def test_empty_source_reports_zero(self):
        async def handler(client, url, **kw):
            return []

        report = await self._run(handler, [Subscription(id="a", name="空源", mode="feed", rss="https://pt/rss")])
        self.assertTrue(report.checks[0].ok)
        self.assertEqual(report.checks[0].item_count, 0)
        self.assertEqual(report.checks[0].previews, [])

    async def test_preview_limit(self):
        async def handler(client, url, **kw):
            return [item(f"条目 {i}") for i in range(20)]

        report = await self._run(handler, [Subscription(id="a", name="多", mode="feed", rss="https://pt/rss")])
        # 默认预览 5 条，但总数要报 20
        self.assertEqual(report.checks[0].item_count, 20)
        self.assertEqual(len(report.checks[0].previews), fc.PREVIEW_LIMIT)

    async def test_no_sources(self):
        report = await self._run(lambda *a, **k: [], [])
        self.assertEqual(report.checks, [])
        self.assertFalse(report.healthy)
        self.assertIn("没有任何启用的 RSS 源", report.render_text())


class RenderTest(unittest.TestCase):
    def _report(self) -> FeedsReport:
        report = FeedsReport()
        report.checks = [
            fc.FeedCheck(
                sub_id="a", sub_name="好源", url="https://pt/rss?passkey=SECRET123",
                ok=True, item_count=37, elapsed=0.42, newest="02-05 20:00",
                previews=[build_preview(item("某剧 S01E05 2160p HEVC", episode=5))],
            ),
            fc.FeedCheck(
                sub_id="b", sub_name="坏源", url="https://pt/bad?passkey=SECRET456",
                ok=False, error="HTTP 401 —— 鉴权失败：passkey 可能已过期", elapsed=1.2,
            ),
        ]
        report.skipped_disabled = 2
        report.finished_at = report.started_at + 1.7
        return report

    def test_text_report(self):
        text = self._report().render_text()
        self.assertIn("订阅源体检报告", text)
        self.assertIn("✅ 正常 1 个", text)
        self.assertIn("❌ 失败 1 个", text)
        self.assertIn("已跳过 2 条停用的订阅", text)
        self.assertIn("37 条", text)
        self.assertIn("S01E05", text)
        self.assertIn("2160p", text)
        self.assertIn("passkey 可能已过期", text)
        self.assertIn("排错建议", text)
        # 密钥必须打码
        self.assertNotIn("SECRET123", text)
        self.assertNotIn("SECRET456", text)
        self.assertIn("passkey=***", text)

    def test_json_report_masks_secrets(self):
        raw = self._report().render_json()
        self.assertNotIn("SECRET123", raw)
        self.assertNotIn("SECRET456", raw)
        data = json.loads(raw)
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["ok"], 1)
        self.assertEqual(data["failed"], 1)
        self.assertEqual(data["skipped_disabled"], 2)
        self.assertEqual(data["checks"][0]["item_count"], 37)
        self.assertEqual(len(data["checks"][0]["previews"]), 1)
        self.assertIn("passkey=***", data["checks"][0]["url"])

    def test_empty_report_text(self):
        text = FeedsReport().render_text()
        self.assertIn("没有任何启用的 RSS 源", text)


class MaskTest(unittest.TestCase):
    def test_masks_various_secret_params(self):
        check = fc.FeedCheck("a", "n", "https://pt/rss?passkey=ABC&torrent_pass=DEF&api_key=GHI&cat=2")
        masked = check.masked_url
        self.assertNotIn("ABC", masked)
        self.assertNotIn("DEF", masked)
        self.assertNotIn("GHI", masked)
        self.assertIn("cat=2", masked, "非密钥参数要保留，方便确认地址对不对")

    def test_no_secret_unchanged(self):
        url = "https://pt/rss?cat=2&limit=50"
        self.assertEqual(fc.FeedCheck("a", "n", url).masked_url, url)


if __name__ == "__main__":
    unittest.main()
