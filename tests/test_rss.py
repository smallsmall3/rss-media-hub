"""RSS 解析与集号识别测试（纯离线，不依赖 httpx / feedparser）。"""

from __future__ import annotations

import unittest

from app.rss import FeedItem, human_size, parse_episode, parse_feed_bytes, parse_size, sort_items

RSS_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:torrent="https://github.com/Jackett/Jackett/raw/master/src/Jackett.Common/Models/IndexerConfig/Bespoke/torznab">
  <channel>
    <title>PT Site</title>
    <item>
      <title>[中字] Some Show S02E05 [1080p][WEB-DL][H264]</title>
      <guid isPermaLink="false">pt-1001</guid>
      <link>https://pt.example/details.php?id=1001&amp;passkey=SECRET1</link>
      <enclosure url="https://pt.example/download.php?id=1001&amp;passkey=SECRET1" length="2362232012" type="application/x-bittorrent"/>
      <pubDate>Mon, 03 Feb 2025 10:20:30 +0800</pubDate>
      <description>Size: 2.2 GiB</description>
    </item>
    <item>
      <title>Some Show S02E06 2160p</title>
      <guid>pt-1002</guid>
      <link>https://pt.example/details.php?id=1002</link>
      <torrent:magnetURI>magnet:?xt=urn:btih:ABCDEF0123456789</torrent:magnetURI>
      <pubDate>Tue, 04 Feb 2025 11:00:00 +0800</pubDate>
    </item>
  </channel>
</rss>
""".encode("utf-8")

ATOM_SAMPLE = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Mikan</title>
  <entry>
    <title>[组] Anime Name - 07 [1080p]</title>
    <id>tag:mikan,2025:07</id>
    <link rel="alternate" href="https://mikan.example/Home/Episode/07"/>
    <updated>2025-02-05T12:00:00Z</updated>
    <summary>magnet:?xt=urn:btih:0123456789ABCDEF &lt;br/&gt; 350 MB</summary>
  </entry>
</feed>
""".encode("utf-8")


class EpisodeParsingTest(unittest.TestCase):
    def test_standard_season_episode(self):
        self.assertEqual(parse_episode("Some Show S02E05 [1080p]"), (2, 5, None))
        self.assertEqual(parse_episode("some.show.s1e12.1080p"), (1, 12, None))
        self.assertEqual(parse_episode("Show 2x07 WEB-DL"), (2, 7, None))

    def test_chinese_episode(self):
        self.assertEqual(parse_episode("庆余年 第二季 第05集"), (2, 5, None))
        self.assertEqual(parse_episode("电视剧 第12话 1080p"), (None, 12, None))

    def test_absolute_numbering(self):
        season, episode, _ = parse_episode("[Sub] Anime Name - 07 [1080p]")
        self.assertIsNone(season)
        self.assertEqual(episode, 7)

    def test_batch_release(self):
        _, _, batch = parse_episode("Some Show 全12集 1080p")
        self.assertEqual(batch, 12)

    def test_resolution_is_not_episode(self):
        _, episode, _ = parse_episode("Some Movie 2160p HDR")
        self.assertNotEqual(episode, 2160)

    def test_episode_label(self):
        item = FeedItem(title="x", season=1, episode=5)
        self.assertEqual(item.episode_label, "S01E05")
        self.assertEqual(FeedItem(title="x", episode=7).episode_label, "E07")
        self.assertEqual(FeedItem(title="x", batch_count=12).episode_label, "全12集")


class SizeParsingTest(unittest.TestCase):
    def test_units(self):
        self.assertEqual(parse_size("2.2 GB"), 2_200_000_000)
        self.assertEqual(parse_size("2.2 GiB"), 2_362_232_012)
        self.assertEqual(parse_size("350 MB"), 350_000_000)
        self.assertEqual(parse_size("1234567"), 1234567)
        self.assertIsNone(parse_size(None))

    def test_human(self):
        self.assertEqual(human_size(2_362_232_012), "2.20 GB")
        self.assertEqual(human_size(None), "-")


class FeedParsingTest(unittest.TestCase):
    def test_rss2(self):
        items = parse_feed_bytes(RSS_SAMPLE)
        self.assertEqual(len(items), 2)
        first = items[0]
        self.assertEqual(first.guid, "pt-1001")
        self.assertIn("passkey=SECRET1", first.download_url)
        self.assertEqual(first.season, 2)
        self.assertEqual(first.episode, 5)
        self.assertEqual(first.size_bytes, 2_362_232_012)
        self.assertEqual(first.episode_label, "S02E05")
        self.assertIsNotNone(first.published)

        second = items[1]
        self.assertTrue(second.download_url.startswith("magnet:"))

    def test_atom(self):
        items = parse_feed_bytes(ATOM_SAMPLE)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.title, "[组] Anime Name - 07 [1080p]")
        self.assertTrue(item.download_url.startswith("magnet:"))
        self.assertEqual(item.episode, 7)

    def test_dedupe_keys_are_stable(self):
        first = parse_feed_bytes(RSS_SAMPLE)
        second = parse_feed_bytes(RSS_SAMPLE)
        self.assertEqual([i.key for i in first], [i.key for i in second])

    def test_empty_feed(self):
        self.assertEqual(parse_feed_bytes(b""), [])

    def test_sort_orders_by_time(self):
        items = sort_items(parse_feed_bytes(RSS_SAMPLE))
        self.assertEqual(items[0].guid, "pt-1001")
        self.assertEqual(items[-1].guid, "pt-1002")

    def test_key_falls_back_to_link(self):
        item = FeedItem(title="no guid", link="https://x/y")
        self.assertEqual(item.key, "https://x/y")


if __name__ == "__main__":
    unittest.main()
