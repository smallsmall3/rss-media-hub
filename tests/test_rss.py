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


class DetailUrlTest(unittest.TestCase):
    """详情页推导：推送里的链接不能带 passkey。

    真实反馈：推送里直接给 PT 直链（含 passkey），转发给别人
    就等于把站点通行证交出去了。
    """

    def test_chdbits_style(self):
        from app.rss import detail_from_download

        got = detail_from_download(
            "https://ptchdbits.co/download.php?id=586947&passkey=6872a37a6b0b11aea7fbf452fbc35ae6"
        )
        self.assertEqual(got, "https://ptchdbits.co/details.php?id=586947")
        self.assertNotIn("passkey", got)

    def test_keeps_non_secret_params(self):
        from app.rss import detail_from_download

        got = detail_from_download("https://pt.example/download.php?id=1&passkey=X&type=1")
        self.assertIn("id=1", got)
        self.assertIn("type=1", got)
        self.assertNotIn("passkey", got)

    def test_path_style(self):
        from app.rss import detail_from_download

        self.assertEqual(
            detail_from_download("https://pt.example/download/123?passkey=Z"),
            "https://pt.example/torrent/123",
        )

    def test_torrents_download_not_double_substituted(self):
        from app.rss import detail_from_download

        self.assertEqual(
            detail_from_download("https://pt.example/torrents/download/55?passkey=Q"),
            "https://pt.example/torrents/55",
        )

    def test_non_download_url_not_rewritten(self):
        from app.rss import detail_from_download

        self.assertEqual(detail_from_download("https://pt.example/details.php?id=1"), "")

    def test_magnet_cannot_be_derived(self):
        from app.rss import detail_from_download

        self.assertEqual(detail_from_download("magnet:?xt=urn:btih:abc"), "")


class FeedItemDetailUrlTest(unittest.TestCase):
    def _item(self, link="", download=""):
        return FeedItem(title="t", link=link, download_url=download)

    def test_prefers_link_when_clean(self):
        it = self._item(
            link="https://pt.example/details.php?id=1",
            download="https://pt.example/download.php?id=1&passkey=SECRET",
        )
        self.assertEqual(it.detail_url, "https://pt.example/details.php?id=1")

    def test_derives_when_link_is_also_download_url(self):
        it = self._item(
            link="https://pt.example/download.php?id=1&passkey=SECRET",
            download="https://pt.example/download.php?id=1&passkey=SECRET",
        )
        self.assertEqual(it.detail_url, "https://pt.example/details.php?id=1")

    def test_never_returns_url_with_secret(self):
        for link, download in (
            ("https://pt.example/download.php?id=1&passkey=S", "https://pt.example/download.php?id=1&passkey=S"),
            ("", "https://pt.example/download.php?id=7&passkey=S"),
            ("", "magnet:?xt=urn:btih:abc"),
            ("https://pt.example/download.php?passkey=S", ""),
        ):
            it = self._item(link=link, download=download)
            self.assertNotIn("passkey", it.detail_url, f"{link} / {download} 泄露了密钥")

    def test_empty_when_nothing_derivable(self):
        self.assertEqual(self._item(link="", download="magnet:?xt=urn:btih:abc").detail_url, "")


class SecretParamCoverageTest(unittest.TestCase):
    """密钥参数名要覆盖足够多的站。

    真实反馈：ubits.club 用的是 `downhash`（一个带签名的 JWT 下载令牌），
    一开始漏掉了 —— 结果这个令牌被当普通参数**带进了详情页链接**。
    """

    def _secret(self):
        from app.rss import _has_secret

        return _has_secret

    def test_downhash_is_secret(self):
        """NexusPHP 系的下载令牌，比 passkey 更隐蔽。"""
        url = (
            "https://ubits.club/download.php?id=366321&downhash=92475%7C"
            "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpZCI6MzY2MzIxLCJleHAiOjE3OTA3NDg3MjB9"
            ".X4P_7Tfc-ngqPMc9--Kr_at0j_Ajlg1MNM_cxegQ3Ek"
        )
        self.assertTrue(self._secret()(url), "downhash 必须被识别为密钥")

    def test_downhash_stripped_from_detail_url(self):
        from app.rss import detail_from_download

        url = (
            "https://ubits.club/download.php?id=366321&downhash=92475%7C"
            "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpZCI6MzY2MzIxLCJleHAiOjE3OTA3NDg3MjB9"
            ".X4P_7Tfc-ngqPMc9--Kr_at0j_Ajlg1MNM_cxegQ3Ek"
        )
        got = detail_from_download(url)
        self.assertEqual(got, "https://ubits.club/details.php?id=366321")
        self.assertNotIn("downhash", got)
        self.assertNotIn("eyJ0eXAi", got, "JWT 片段绝不能剩下")

    def test_common_pt_secret_params(self):
        for param in (
            "passkey", "passphrase", "torrent_pass", "authkey", "downhash",
            "down_hash", "downkey", "api_key", "apikey", "token", "access_token",
            "c_secure_pass", "sign", "signature", "sid", "session",
        ):
            url = f"https://pt.example/download.php?id=1&{param}=SECRET"
            self.assertTrue(self._secret()(url), f"{param} 应该被识别为密钥")

    def test_plain_params_not_flagged(self):
        """普通参数不能被误判，否则详情页会丢掉有用的信息。"""
        for url in (
            "https://pt.example/details.php?id=1&type=2",
            "https://pt.example/details.php?id=1&page=3",
        ):
            self.assertFalse(self._secret()(url), f"{url} 不该被判为带密钥")