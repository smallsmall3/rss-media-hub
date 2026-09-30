"""标题解析测试：从 PT 发布标题里还原「片名 + 年份」。

这是"给每条推送配海报"的前置能力 —— feed 模式的条目没有 TMDB ID，
必须先把标题还原成能搜索的片名。

测试重点放在**容易切错的地方**：片名自带点号、片名里含年份、
技术串的两种写法（H.265 / H265）、中文标题。
"""

from __future__ import annotations

import unittest

from app.titleparse import (
    ParsedTitle,
    aliases,
    parse_release_title,
    search_query,
    search_terms,
)


class BasicTest(unittest.TestCase):
    def test_movie_with_year(self):
        p = parse_release_title("超新星.2020.1080p.BluRay.x264-GROUP")
        self.assertEqual(p.title, "超新星")
        self.assertEqual(p.year, 2020)
        self.assertTrue(p.confident)

    def test_chinese_colon_in_title(self):
        p = parse_release_title("金蝉脱壳3：恶魔车站.2019.2160p.WEB-DL.HDR.HEVC.DDP5.1-FGT")
        self.assertEqual(p.title, "金蝉脱壳3：恶魔车站")
        self.assertEqual(p.year, 2019)

    def test_english_movie(self):
        p = parse_release_title("The.Matrix.1999.2160p.UHD.BluRay.REMUX.HDR.HEVC.TrueHD.7.1.Atmos")
        self.assertEqual(p.title, "The Matrix")
        self.assertEqual(p.year, 1999)

    def test_episode_title_has_no_year(self):
        p = parse_release_title("某剧.S01E05.2160p.HEVC.HDR.WEB-DL")
        self.assertEqual(p.title, "某剧")
        self.assertIsNone(p.year)
        self.assertEqual((p.season, p.episode), (1, 5))

    def test_chinese_episode_marker(self):
        p = parse_release_title("某动画 第12集 1080p")
        self.assertEqual(p.title, "某动画")
        self.assertEqual(p.episode, 12)


class TrickyTitleTest(unittest.TestCase):
    """片名本身带点号/数字的，最容易切错。"""

    def test_title_with_dots_kept(self):
        """S.W.A.T. 不能变成 'W A T'（曾经真的错成这样）。"""
        p = parse_release_title("S.W.A.T.2017.1080p.WEB-DL.H264")
        self.assertEqual(p.title, "S W A T")
        self.assertEqual(p.year, 2017)

    def test_title_starting_with_numbers(self):
        """3.10.to.Yuma 不能变成 'to Yuma'。"""
        p = parse_release_title("3.10.to.Yuma.2007.1080p.BluRay.x264")
        self.assertEqual(p.title, "3 10 to Yuma")
        self.assertEqual(p.year, 2007)

    def test_movie_name_contains_year_like_token(self):
        """2001.A.Space.Odyssey —— 前面的 2001 不能被当成发行年份。"""
        p = parse_release_title("2001.A.Space.Odyssey.1968.2160p.UHD.BluRay.REMUX.HEVC")
        self.assertEqual(p.title, "2001 A Space Odyssey")
        self.assertEqual(p.year, 1968, "应该取最后一个年份段作为发行年份")

    def test_hyphen_in_title_preserved(self):
        p = parse_release_title("Spider-Man.No.Way.Home.2021.2160p.WEB-DL")
        self.assertEqual(p.title, "Spider-Man No Way Home")
        self.assertEqual(p.year, 2021)

    def test_roman_numeral(self):
        p = parse_release_title("Wandering.Earth.II.2023.2160p.WEB-DL")
        self.assertEqual(p.title, "Wandering Earth II")
        self.assertEqual(p.year, 2023)


class TechTokenTest(unittest.TestCase):
    """技术串的两种写法都要能剥掉。"""

    def test_dotted_h265(self):
        p = parse_release_title("Oppenheimer.2023.2160p.WEB-DL.DDP5.1.Atmos.HDR.H.265")
        self.assertEqual(p.title, "Oppenheimer")

    def test_plain_h265(self):
        p = parse_release_title("流浪地球2.2023.2160p.WEB-DL.H265.10bit.DDP5.1")
        self.assertEqual(p.title, "流浪地球2")
        self.assertEqual(p.year, 2023)

    def test_dts_hd_ma(self):
        p = parse_release_title("Interstellar.2014.2160p.UHD.BluRay.REMUX.HDR.HEVC.DTS-HD.MA.5.1")
        self.assertEqual(p.title, "Interstellar")
        self.assertEqual(p.year, 2014)

    def test_bracket_tags_removed(self):
        p = parse_release_title("[中字] 奥特曼.2022.1080p.BluRay.x264")
        self.assertEqual(p.title, "奥特曼")

    def test_chinese_bracket_tags_removed(self):
        p = parse_release_title("【高清】封神第一部.2023.2160p.WEB-DL")
        self.assertEqual(p.title, "封神第一部")

    def test_no_tech_marks_at_all(self):
        """没有任何规格标记的标题也要能用（剧集常见）。"""
        p = parse_release_title("Some.Random.Thing")
        self.assertEqual(p.title, "Some Random Thing")


class RefuseToGuessTest(unittest.TestCase):
    """宁缺勿错：拿不准就别乱给片名，免得搜出一张不相干的图。"""

    def test_pure_tech_string_returns_empty(self):
        p = parse_release_title("1080p")
        self.assertEqual(p.title, "", "全是规格的标题不该给出片名")
        self.assertFalse(p.confident)

    def test_empty_input(self):
        p = parse_release_title("")
        self.assertEqual(p.title, "")
        self.assertEqual(p.year, None)

    def test_no_tech_means_not_confident(self):
        """没剥掉任何规格 → 大概率不是发布标题，标记为没把握。"""
        p = parse_release_title("一些随便写的文字")
        self.assertFalse(p.confident)


class SearchQueryTest(unittest.TestCase):
    def test_returns_title_only(self):
        self.assertEqual(search_query("超新星.2020.1080p.BluRay.x264"), "超新星")

    def test_returns_empty_when_unparsable(self):
        self.assertEqual(search_query("1080p"), "")


class SerializationTest(unittest.TestCase):
    def test_to_dict(self):
        p = parse_release_title("某剧.S01E05.1080p")
        d = p.to_dict()
        self.assertEqual(d["title"], "某剧")
        self.assertEqual(d["season"], 1)
        self.assertEqual(d["episode"], 5)
        self.assertIn("confident", d)

    def test_str_is_readable(self):
        p = parse_release_title("超新星.2020.1080p")
        self.assertIn("超新星", str(p))
        self.assertIn("2020", str(p))


class IdempotentTest(unittest.TestCase):
    """解析结果里不该再含技术串（否则说明剥得不干净）。"""

    def test_second_pass_finds_no_more_tech(self):
        for raw in (
            "The.Matrix.1999.2160p.UHD.BluRay.REMUX.HDR.HEVC.TrueHD.7.1.Atmos",
            "Oppenheimer.2023.2160p.WEB-DL.DDP5.1.Atmos.HDR.H.265",
            "Dune.Part.Two.2024.2160p.WEB-DL.DDP5.1.Atmos.DV.HDR.H.265",
        ):
            first = parse_release_title(raw)
            again = parse_release_title(first.title)
            self.assertEqual(again.title, first.title, f"{raw} 剥得不干净：{first.title}")


class RealWorldTitleTest(unittest.TestCase):
    """用户实际推送里出现的标题 —— 这些是最真实的回归用例。

    下面第一条来自真实反馈：剧集标题没有年份，且带 `[TV Series]` 前缀、
    `Apple TV+` 平台名。当时的 bug 是把整串
    「Brothers S01 2160p Apple TV」当成了片名，拿去搜 TMDB 必然搜不到，
    所以推送里没有海报。
    """

    def test_chdbits_tv_series_no_year(self):
        raw = "[TV Series]Brothers S01 2160p Apple TV+ WEB-DL DDP.5.1 Atmos HDR10+ H.265-CHD"
        p = parse_release_title(raw)
        self.assertEqual(p.title, "Brothers", "不能把季号/分辨率/平台名混进片名")
        self.assertIsNone(p.year)
        self.assertTrue(p.confident, "能解析出片名就该给海报一次机会")

    def test_platform_name_not_part_of_title(self):
        p = parse_release_title("Some.Show.S02.2160p.NF.WEB-DL.DDP5.1")
        self.assertEqual(p.title, "Some Show")

    def test_season_marker_is_boundary(self):
        """S01 要能当"规格开始"的分界，否则片名会被污染。"""
        p = parse_release_title("True.Detective.S01E01.1080p.BluRay.x264")
        self.assertEqual(p.title, "True Detective")

    def test_bare_resolution_number_stripped(self):
        """有的站不写 p，直接 2160。"""
        p = parse_release_title("某电影 2020 2160 WEB-DL HEVC")
        self.assertEqual(p.title, "某电影")
        self.assertEqual(p.year, 2020)

    def test_nested_brackets_do_not_leak_into_title(self):
        """`[动漫(Animations)]` 这种括号套括号，不能在前面留下 `]`。

        真实反馈踩到过：简单的 `[^\\]】)）]*` 会在内层 `)` 处提前结束匹配。
        """
        raw = "[动漫(Animations)]Su Dong Po Yu Hang Zhou De Gu Shi 2026 S01E32 2160p WEB-DL HDR HEVC AAC-CHD"
        p = parse_release_title(raw)
        self.assertEqual(p.title, "Su Dong Po Yu Hang Zhou De Gu Shi")
        self.assertNotIn("]", p.title)
        self.assertNotIn("(", p.title)

    def test_year_before_season_marker_is_extracted(self):
        """年份落在季号左边时也要能提取出来，并把季号从片名里剥掉。"""
        p = parse_release_title("Another Show S03 2022 1080p WEB-DL")
        self.assertEqual(p.title, "Another Show")
        self.assertEqual(p.year, 2022)

    def test_leading_year_of_title_is_kept(self):
        """`2001 A Space Odyssey` 开头的 2001 是片名，不能当发行年份切掉。"""
        p = parse_release_title("2001.A.Space.Odyssey.1968.2160p.UHD.BluRay.REMUX.HEVC")
        self.assertEqual(p.title, "2001 A Space Odyssey")
        self.assertEqual(p.year, 1968)


class NasRealTitlesTest(unittest.TestCase):
    """从 NAS 实际抓取到的标题（UBits / CHDBits）—— 最有价值的回归用例。

    这些标题暴露了一个关键 bug：片名区尾部残留技术段
    （`... Gu Shi 2160p HDRVivid`），拿去搜 TMDB 搜不到，海报就一直没有。
    原因是我的右往左剥离遇到当时不在词表里的 `HDRVivid` 就停住了。
    """

    def test_ubits_anime_with_hdrvivid(self):
        raw = ("[动漫(Animations)]Su Dong Po Yu Hang Zhou De Gu Shi 2026 "
               "S01E32 2160p WEB-DL HDRVivid H265 AAC-UBWEB")
        p = parse_release_title(raw)
        self.assertEqual(p.title, "Su Dong Po Yu Hang Zhou De Gu Shi")
        self.assertEqual(p.year, 2026)
        self.assertNotIn("2160p", p.title, "分辨率不能留在片名里")
        self.assertNotIn("HDRVivid", p.title)

    def test_ubits_anime_group_suffix_not_in_title(self):
        raw = "[动漫(Animations)]Raised by Demons Panda Li 2026 S01E12 2160p WEB-DL H264 AAC-UBWEB"
        p = parse_release_title(raw)
        self.assertEqual(p.title, "Raised by Demons Panda Li")
        self.assertNotIn("UBWEB", p.title, "站点标识残片不能进片名")

    def test_ubits_long_running_anime(self):
        p = parse_release_title(
            "[动漫(Animations)]Swallowed Star 2020 S01E243 2160p WEB-DL H265 AAC-UBWEB"
        )
        self.assertEqual(p.title, "Swallowed Star")
        self.assertEqual(p.year, 2020)

    def test_hq_marker_stripped(self):
        p = parse_release_title(
            "[动漫(Animations)]Gu An 2026 S01E11 2160p WEB-DL HQ HDR10 H265 10bit AAC-UBWEB"
        )
        self.assertEqual(p.title, "Gu An")
        self.assertEqual(p.year, 2026)

    def test_chdbits_apple_tv_series(self):
        for raw, want in (
            ("[TV Series]Last Seen S01 2160p Apple TV+ WEB-DL DDP.5.1 Atmos DV H.265-CHDWEB", "Last Seen"),
            ("[TV Series]Women in Blue S02 2160p Apple TV+ WEB-DL DDP.5.1 Atmos DV H.265-CHDWEB", "Women in Blue"),
            ("[TV Series]Brothers S01 2160p Apple TV+ WEB-DL DDP.5.1 Atmos DV H.265-CHDWEB", "Brothers"),
        ):
            p = parse_release_title(raw)
            self.assertEqual(p.title, want, raw)

    def test_normal_words_not_treated_as_country_codes(self):
        """`De` 曾被当成德国国家码删掉，导致片名少一个词。"""
        p = parse_release_title("Su Dong Po Yu Hang Zhou De Gu Shi 2026 S01E32 2160p")
        self.assertIn("De", p.title)

    def test_single_letter_and_numeric_titles_survive(self):
        """片名里的单字母与数字不能被当技术段删掉（实测错过）。"""
        self.assertEqual(parse_release_title("S.W.A.T.2017.1080p.WEB-DL.H264").title, "S W A T")
        self.assertEqual(parse_release_title("3.10.to.Yuma.2007.1080p.BluRay.x264").title, "3 10 to Yuma")
        self.assertEqual(
            parse_release_title("2001.A.Space.Odyssey.1968.2160p.UHD.BluRay.REMUX.HEVC").title,
            "2001 A Space Odyssey",
        )


class ChineseAliasTest(unittest.TestCase):
    """中文别名提取：国产动漫常用拼音当标题，TMDB 上只有中文条目。

    真实案例：`Su Dong Po Yu Hang Zhou De Gu Shi` 在 TMDB 搜 0 条
    （拼音没收录），但标题尾部方括号里有中文名，用中文能搜到。
    """

    def test_alias_from_bracket(self):
        raw = ("[动漫(Animations)]Swallowed Star 2020 S01E243 2160p WEB-DL H265 AAC-UBWEB"
               "[吞噬星空 | 第243集 | 导演: 沈乐平]")
        got = aliases(raw)
        self.assertIn("吞噬星空", got)
        self.assertNotIn("沈乐平", got, "导演名不该被当别名")
        self.assertNotIn("第243集", got)

    def test_alias_with_slash(self):
        raw = ("[动漫(Animations)]Gu An 2026 S01E11 2160p WEB-DL H265 AAC-UBWEB"
               "[一斩苍穹/一斩苍穹3D动画]")
        got = aliases(raw)
        self.assertIn("一斩苍穹", got)

    def test_classification_tag_is_not_alias(self):
        """`[动漫(Animations)]` / `[TV Series]` 是分类标签，不是片名。"""
        self.assertEqual(aliases("[动漫(Animations)]Some Show 2024 S01E01 1080p"), [])
        self.assertEqual(aliases("[TV Series]Some Show S01 1080p"), [])

    def test_chinese_drama_prefix_stripped(self):
        raw = "[TV Series]Last Seen S01 2160p WEB-DL H.265-CHDWEB[澳剧：最后目击 第一季 第5集]"
        got = aliases(raw)
        self.assertIn("最后目击", got)
        self.assertNotIn("澳剧", " ".join(got))
        self.assertNotIn("第一季", " ".join(got))

    def test_body_chinese_used_as_alias(self):
        p = aliases("某剧.S01E05.2160p.HEVC.HDR.WEB-DL")
        self.assertIn("某剧", p)

    def test_no_alias_for_pure_latin_title(self):
        self.assertEqual(aliases("The.Matrix.1999.2160p.UHD.BluRay.REMUX"), [])

    def test_single_cjk_char_rejected(self):
        """一个汉字太短，容易搜错，宁可不给。"""
        self.assertEqual(aliases("[某] Some Show 2024 1080p"), [])


class SearchTermsTest(unittest.TestCase):
    def test_primary_term_is_clean_title(self):
        raw = ("[动漫(Animations)]Raised by Demons Panda Li 2026 S01E12 2160p WEB-DL "
               "H264 AAC-UBWEB[李熊猫/李熊猫与恶魔]")
        terms = search_terms(raw)
        self.assertEqual(terms[0], "Raised by Demons Panda Li", "主搜索词不能带技术段")
        self.assertIn("李熊猫", terms)

    def test_latin_title_only_gives_one_term(self):
        self.assertEqual(search_terms("The.Matrix.1999.2160p.UHD.BluRay"), ["The Matrix"])

    def test_terms_have_no_duplicates(self):
        raw = "某剧.S01E01.1080p[某剧]"
        terms = search_terms(raw)
        self.assertEqual(len(terms), len(set(terms)))

    def test_ubweb_group_not_in_primary_term(self):
        """`-UBWEB` 曾被当成内容词，导致剥离提前停住、技术段残留。"""
        raw = ("[动漫(Animations)]Swallowed Star 2020 S01E243 2160p WEB-DL H265 AAC-UBWEB"
               "[吞噬星空 | 第243集]")
        self.assertEqual(search_terms(raw)[0], "Swallowed Star")


if __name__ == "__main__":
    unittest.main()
