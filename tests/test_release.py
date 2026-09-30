"""发布信息提取与通知排版测试。

release.py 的提取规则必须"宁缺勿错"：标错分辨率比不标更糟，
因为用户会拿这些标签做过滤决策。
"""

from __future__ import annotations

import unittest

from app.config import Subscription
from app.notify import ItemView, Notifier
from app.release import classify_item, describe_release, extract_group


def view(title: str, ep: str = "", size: str = "1.00 GB") -> ItemView:
    tags = describe_release(title)
    kind, icon = classify_item(title, has_episode=bool(ep))
    return ItemView(
        title=title,
        download_url="https://pt.example/dl?passkey=SECRET",
        # 真实链路里 FeedItem.detail_url 会算出"去掉密钥"的兜底地址；
        # 这里手工构造时也要给上，否则测的不是真实行为。
        detail_url="https://pt.example/dl",
        size_text=size,
        episode_label=ep,
        kind=kind,
        icon=icon,
        badges=tags.badges(),
        rank=tags.quality_rank(),
    )


class ResolutionTest(unittest.TestCase):
    def test_common_resolutions(self):
        cases = {
            "Show S01E01 4320p": "4320p",
            "Show S01E01 2160p WEB-DL": "2160p",
            "Show S01E01 4K HDR": "2160p",
            "Show S01E01 1080p BluRay": "1080p",
            "Show S01E01 1080i HDTV": "1080i",
            "Show S01E01 720p": "720p",
            "Show S01E01 480p": "480p",
        }
        for title, expected in cases.items():
            self.assertEqual(describe_release(title).resolution, expected, title)

    def test_does_not_match_numbers_inside_other_numbers(self):
        # 12160p / 21080p 这种不该被当成分辨率
        self.assertEqual(describe_release("Show 12160p").resolution, "")
        self.assertEqual(describe_release("Show x1080p9").resolution, "")

    def test_2160p_wins_over_4k(self):
        self.assertEqual(describe_release("Show 2160p 4K").resolution, "2160p")


class HdrTest(unittest.TestCase):
    def test_dolby_vision(self):
        self.assertEqual(describe_release("Show 2160p DV HDR").hdr, "杜比视界")
        self.assertEqual(describe_release("Show 2160p Dolby Vision").hdr, "杜比视界")

    def test_hdr10_plus_beats_hdr10(self):
        self.assertEqual(describe_release("Show 2160p HDR10+").hdr, "HDR10+")
        self.assertEqual(describe_release("Show 2160p HDR10").hdr, "HDR10")

    def test_plain_hdr(self):
        self.assertEqual(describe_release("Show 2160p HDR").hdr, "HDR")

    def test_dv_is_not_matched_inside_words(self):
        # "dvd" 里的 dv 不算杜比视界
        self.assertEqual(describe_release("Show 1080p DVDRip x264").hdr, "")


class CodecAudioSourceTest(unittest.TestCase):
    def test_video_codec(self):
        self.assertEqual(describe_release("Show 2160p HEVC").video, "HEVC")
        self.assertEqual(describe_release("Show 1080p x265").video, "HEVC")
        self.assertEqual(describe_release("Show 1080p H.265").video, "HEVC")
        self.assertEqual(describe_release("Show 1080p x264").video, "AVC")
        self.assertEqual(describe_release("Show 1080p H.264").video, "AVC")
        self.assertEqual(describe_release("Show 1080p AV1").video, "AV1")

    def test_audio(self):
        self.assertEqual(describe_release("Show TrueHD Atmos").audio, "TrueHD")
        self.assertEqual(describe_release("Show DTS-HD MA").audio, "DTS-HD")
        self.assertEqual(describe_release("Show DDP5.1").audio, "DDP")
        self.assertEqual(describe_release("Show EAC3").audio, "DDP")
        self.assertEqual(describe_release("Show FLAC").audio, "FLAC")

    def test_source(self):
        self.assertEqual(describe_release("Show 2160p BluRay REMUX").source, "原盘")
        self.assertEqual(describe_release("Show 1080p BluRay x264").source, "BluRay")
        self.assertEqual(describe_release("Show 1080p WEB-DL").source, "WEB-DL")
        self.assertEqual(describe_release("Show 1080p WEBRip").source, "WEBRip")
        self.assertEqual(describe_release("Show 720p HDTV").source, "HDTV")

    def test_langs(self):
        tags = describe_release("[国语] 某剧 S01E01 粤语中字")
        self.assertIn("国语", tags.langs)
        self.assertIn("粤语", tags.langs)
        self.assertIn("中字", tags.langs)

    def test_empty_title(self):
        tags = describe_release("")
        self.assertTrue(tags.is_empty)
        self.assertEqual(tags.badges(), [])


class GroupTest(unittest.TestCase):
    def test_extracts_group(self):
        self.assertEqual(extract_group("Show S01E01 1080p WEB-DL-XXX"), "XXX")
        self.assertEqual(extract_group("Movie.2024.2160p-OurBits"), "OurBits")

    def test_does_not_treat_specs_as_group(self):
        # "-1080p" 这种尾巴是规格，不是压制组
        for title in (
            "Show S01E01-1080p",
            "Show S01E01 2160p",
            "Show-HEVC",
            "Show-x264",
            "Show 2024",
            "Show-720p",
        ):
            self.assertEqual(extract_group(title), "", f"{title} 不该解析出压制组")

    def test_no_group(self):
        self.assertEqual(extract_group("Show S01E01 1080p"), "")
        self.assertEqual(extract_group(""), "")


class BadgesTest(unittest.TestCase):
    def test_badge_order_and_limit(self):
        tags = describe_release("[中字]某剧.S01E05.2160p.WEB-DL.HDR.HEVC.DDP5.1-XXX")
        badges = tags.badges()
        self.assertEqual(badges[:4], ["2160p", "HDR", "HEVC", "WEB-DL"])
        self.assertLessEqual(len(tags.badges(limit=2)), 2)

    def test_quality_rank_orders_versions(self):
        uhd_hdr = describe_release("Show 2160p HDR HEVC")
        fhd = describe_release("Show 1080p x264")
        sd = describe_release("Show 480p")
        self.assertGreater(uhd_hdr.quality_rank(), fhd.quality_rank())
        self.assertGreater(fhd.quality_rank(), sd.quality_rank())

    def test_dv_ranks_above_plain_hdr(self):
        dv = describe_release("Show 2160p DV")
        hdr10 = describe_release("Show 2160p HDR10")
        self.assertGreater(dv.quality_rank(), hdr10.quality_rank())


class ClassifyTest(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(classify_item("Show S01E01 1080p", has_episode=True)[0], "剧集")
        self.assertEqual(classify_item("Movie.2024.2160p.WEB-DL")[0], "电影")
        self.assertEqual(classify_item("某剧 全12集 1080p")[0], "合集")
        self.assertEqual(classify_item("Album 2024 FLAC 24bit")[0], "音乐")
        self.assertEqual(classify_item("软件包 绿色版")[0], "软件")
        self.assertEqual(classify_item("某书 epub")[0], "图书")
        self.assertEqual(classify_item("完全看不出来的标题")[0], "资源")

    def test_episode_flag_wins_over_music(self):
        # 剧集标题里出现 FLAC（比如音轨规格）不该被当成音乐
        kind, _ = classify_item("Show S01E01 1080p FLAC", has_episode=True)
        self.assertEqual(kind, "剧集")


class NotifierLayoutTest(unittest.TestCase):
    def setUp(self):
        self.notifier = Notifier.__new__(Notifier)
        self.sub = Subscription(id="t", name="我的 PT 站", mode="feed", rss="https://pt.example/rss")

    def test_groups_same_episode_versions(self):
        views = [
            view("某剧 S01E05 720p", "S01E05", "1.10 GB"),
            view("某剧 S01E05 1080p", "S01E05", "3.20 GB"),
            view("某剧 S01E05 2160p HDR HEVC", "S01E05", "12.50 GB"),
        ]
        groups = self.notifier.group_by_episode(views)
        self.assertEqual(len(groups), 1)
        label, group = groups[0]
        self.assertEqual(label, "S01E05")
        # 规格最高的排最前
        self.assertEqual(group[0].size_text, "12.50 GB")

    def test_message_mentions_version_count(self):
        text = self.notifier.render_feed_items(
            self.sub,
            [view("某剧 S01E05 720p", "S01E05"), view("某剧 S01E05 2160p", "S01E05")],
        )
        self.assertIn("1 集 / 2 个版本", text)
        self.assertIn("S01E05", text)
        self.assertIn("2160p", text)
        self.assertIn("还有", text)

    def test_single_item_shows_full_title(self):
        text = self.notifier.render_feed_items(
            self.sub, [view("[组] 番剧 - 85 [1080p][简繁中字]", "E85", "1.40 GB")]
        )
        self.assertIn("E85", text)
        self.assertIn("1.40 GB", text)
        self.assertIn("番剧 - 85", text, "单条推送应包含完整标题")
        self.assertIn("1080p", text)

    def test_movie_uses_title_as_headline(self):
        """没有集号时，标题必须当标题用，不能只显示"电影"两个字。"""
        text = self.notifier.render_feed_items(
            self.sub, [view("某电影.2024.2160p.UHD.BluRay.Remux.DV.HDR-OurBits", "", "58.00 GB")]
        )
        self.assertIn("某电影.2024", text)
        self.assertIn("58.00 GB", text)
        self.assertNotIn("<b>电影</b>", text)

    def test_html_is_escaped(self):
        text = self.notifier.render_feed_items(self.sub, [view("Show <script> S01E01", "S01E01")])
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)

    def test_download_url_never_leaks_passkey(self):
        """推送里的链接不能带 passkey —— 转发或截图就等于泄露站点通行证。

        原来这条断言的是"href 里要有 &amp;passkey=SECRET"，
        那是刻意把密钥往外发的行为，已经改成默认指向详情页了。
        """
        # 用真实的 FeedItem → ItemView 转换，才能测到 detail_url 的推导
        from app.rss import FeedItem

        feed_item = FeedItem(
            title="某剧 S01E01",
            link="",
            download_url="https://pt.example/dl?a=1&passkey=SECRET",
        )
        item = ItemView.from_feed(feed_item)
        text = self.notifier.render_feed_items(self.sub, [item])
        self.assertNotIn("passkey", text, "绝不能把 passkey 写进推送")
        self.assertNotIn("SECRET", text)
        # 兜底：去掉密钥参数后的地址仍然可用，不能干脆不给链接
        self.assertIn("https://pt.example/dl?a=1", text)

    def test_link_uses_detail_page(self):
        """有详情页时优先用详情页（谁点都得先登录）。"""
        item = view("某剧 S01E01", "S01E01")
        item.download_url = "https://pt.example/download.php?id=1&passkey=SECRET"
        item.detail_url = "https://pt.example/details.php?id=1"
        text = self.notifier.render_feed_items(self.sub, [item])
        self.assertIn("details.php?id=1", text)
        self.assertNotIn("passkey", text)
        self.assertIn("查看", text, "详情页模式的文案是「查看」而不是「下载」")

    def test_download_mode_keeps_clean_direct_link(self):
        """配成 download 模式时，不带密钥的直链要保留（真能一键下载）。"""
        notifier = Notifier.__new__(Notifier)
        notifier.settings = type("S", (), {"telegram": type("t", (), {"link_mode": "download"})()})
        clean = type("I", (), {"detail_url": "", "download_url": "https://pt.example/download.php?id=1"})()
        self.assertEqual(notifier.link_of(clean), "https://pt.example/download.php?id=1")

    def test_download_mode_still_refuses_secret_link(self):
        """即使配成 download 模式，带密钥的直链也不能发。"""
        notifier = Notifier.__new__(Notifier)
        notifier.settings = type("S", (), {"telegram": type("t", (), {"link_mode": "download"})()})
        leaky = type("I", (), {
            "detail_url": "https://pt.example/details.php?id=1",
            "download_url": "https://pt.example/download.php?id=1&passkey=SECRET",
        })()
        self.assertEqual(notifier.link_of(leaky), "https://pt.example/details.php?id=1")

    def test_empty_views(self):
        text = self.notifier.render_feed_items(self.sub, [])
        self.assertIn("我的 PT 站", text)

    def test_note_rendered(self):
        sub = Subscription(id="t", name="源", mode="feed", rss="https://x/rss", note="只看 2160p")
        text = self.notifier.render_feed_items(sub, [view("某剧 S01E01 2160p", "S01E01")])
        self.assertIn("只看 2160p", text)

    def test_show_mode_keeps_progress_line(self):
        """show 模式的通知必须保留入库进度，不能被 feed 排版改坏。"""
        from app.reconcile import MissingEpisode, ReconcileResult

        result = ReconcileResult(ok=True, total=10, aired=10, owned=7)
        result.missing = [MissingEpisode(season=1, episode=i) for i in (8, 9, 10)]
        sub = Subscription(id="s", name="某剧", mode="show", tmdb_id=1, rss="https://x/rss")
        text = self.notifier.render_new_items(sub, [view("某剧 S01E05 1080p", "S01E05")], result)
        self.assertIn("7/10", text)
        self.assertIn("待入库 3 集", text)
        self.assertIn("S01E08-E10", text)

    def test_library_update_shows_missing_count(self):
        from app.reconcile import MissingEpisode, ReconcileResult

        result = ReconcileResult(ok=True, total=6, aired=6, owned=5)
        result.missing = [MissingEpisode(season=2, episode=3)]
        text = self.notifier.render_library_update(
            self.sub, result, ["S02E02"], [view("某剧 S02E02 1080p", "S02E02")]
        )
        self.assertIn("5/6", text)
        self.assertIn("S02E03", text)
        self.assertIn("S02E02", text)

    def test_long_title_truncated(self):
        long_title = "某" * 200 + " S01E01"
        text = self.notifier.render_feed_items(self.sub, [view(long_title, "")])
        self.assertLess(len(text), 600)


if __name__ == "__main__":
    unittest.main()
