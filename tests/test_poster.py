"""海报解析测试。

重点是三道「宁缺勿错」的闸门，因为没有人工确认，
一旦搜错就会推一张完全不相干的图到你手机上：
  1. 标题解析没把握 → 不给海报
  2. 没搜到结果 → 不给海报
  3. 结果和片名不够像 → 不给海报
"""

from __future__ import annotations

import unittest

from app.poster import PosterResolver, normalize_for_compare, similarity


class NormalizeTest(unittest.TestCase):
    def test_strips_punctuation_and_case(self):
        self.assertEqual(normalize_for_compare("The Matrix!"), "thematrix")
        self.assertEqual(normalize_for_compare("超新星 · 第一季"), "超新星第一季")

    def test_keeps_cjk(self):
        self.assertEqual(normalize_for_compare("流浪地球2"), "流浪地球2")

    def test_empty(self):
        self.assertEqual(normalize_for_compare(""), "")
        self.assertEqual(normalize_for_compare(None), "")


class SimilarityTest(unittest.TestCase):
    def test_identical(self):
        self.assertEqual(similarity("超新星", "超新星"), 1.0)
        self.assertEqual(similarity("The Matrix", "The Matrix"), 1.0)

    def test_case_and_punctuation_insensitive(self):
        self.assertEqual(similarity("the matrix", "The.Matrix"), 1.0)

    def test_containment_scores_high(self):
        self.assertGreater(similarity("The Matrix", "Matrix"), 0.7)
        self.assertGreater(similarity("超新星", "超新星 第一季"), 0.7)

    def test_unrelated_scores_below_threshold(self):
        """无关片名的分数必须低于采纳阈值 —— 这才是真正要保证的事。

        （不必苛求接近 0：像 Oppenheimer / Friends 这种共享字母的，
        拿到的零点几分无所谓，只要低于阈值就会被拒绝。）
        """
        from app.poster import MIN_SIMILARITY

        self.assertLess(similarity("超新星", "完全无关的剧"), MIN_SIMILARITY)
        self.assertLess(similarity("Oppenheimer", "Friends"), MIN_SIMILARITY)

    def test_similar_titles_above_threshold(self):
        """正常能匹配上的必须高于阈值，否则海报功能就形同虚设。"""
        from app.poster import MIN_SIMILARITY

        for a, b in (
            ("超新星", "超新星"),
            ("The Matrix", "Matrix"),
            ("The Matrix", "The Matrix"),
            ("超新星", "超新星 第一季"),
            ("Dune", "Dune: Part Two"),
            ("Oppenheimer", "The Oppenheimer Story"),
        ):
            self.assertGreaterEqual(similarity(a, b), MIN_SIMILARITY, f"{a!r} vs {b!r} 应该能匹配")

    def test_empty_is_zero(self):
        self.assertEqual(similarity("", "x"), 0.0)
        self.assertEqual(similarity("x", ""), 0.0)


class PickTest(unittest.TestCase):
    """_pick 从搜索结果里挑一个，不够像就返回 None。"""

    def setUp(self):
        # 绕过 __init__ 的 httpx 依赖，只测挑选逻辑
        self.r = PosterResolver.__new__(PosterResolver)

    def test_picks_exact_match(self):
        results = [
            {"name": "超新星纪元", "original_name": "", "first_air_date": "2024-01-01", "poster_path": "/wrong.jpg"},
            {"name": "超新星", "original_name": "", "first_air_date": "2020-05-01", "poster_path": "/right.jpg"},
        ]
        picked = self.r._pick(results, "超新星", 2020)
        self.assertIsNotNone(picked)
        self.assertEqual(picked["poster_path"], "/right.jpg")

    def test_returns_none_when_nothing_similar(self):
        results = [{"name": "完全无关的剧", "original_name": "", "first_air_date": "2019-01-01", "poster_path": "/x.jpg"}]
        self.assertIsNone(self.r._pick(results, "超新星", 2020), "不够像时必须放弃，不能硬配一张图")

    def test_returns_none_for_empty_results(self):
        self.assertIsNone(self.r._pick([], "超新星", None))
        self.assertIsNone(self.r._pick(None, "超新星", None))

    def test_year_breaks_tie_between_same_names(self):
        results = [
            {"name": "超新星", "original_name": "", "first_air_date": "1999-01-01", "poster_path": "/old.jpg"},
            {"name": "超新星", "original_name": "", "first_air_date": "2020-01-01", "poster_path": "/new.jpg"},
        ]
        picked = self.r._pick(results, "超新星", 2020)
        self.assertEqual(picked["poster_path"], "/new.jpg")

    def test_year_mismatch_penalized(self):
        """年份差很多的结果要被压下去。"""
        results = [
            {"name": "超新星", "original_name": "", "first_air_date": "1950-01-01", "poster_path": "/ancient.jpg"},
            {"name": "超新星", "original_name": "", "first_air_date": "2020-01-01", "poster_path": "/new.jpg"},
        ]
        picked = self.r._pick(results, "超新星", 2020)
        self.assertEqual(picked["poster_path"], "/new.jpg")

    def test_uses_original_name_too(self):
        """中文名对不上，但原名对得上，也要能匹配。"""
        results = [
            {"name": "某中文译名", "original_name": "The Matrix", "first_air_date": "1999-03-31", "poster_path": "/m.jpg"},
        ]
        picked = self.r._pick(results, "The Matrix", 1999)
        self.assertIsNotNone(picked)
        self.assertEqual(picked["poster_path"], "/m.jpg")


class YearParseTest(unittest.TestCase):
    def test_parses_year(self):
        from app.poster import _year_of

        self.assertEqual(_year_of("2020-05-01"), 2020)
        self.assertEqual(_year_of("1999-03-31"), 1999)

    def test_handles_missing_or_bad(self):
        from app.poster import _year_of

        self.assertIsNone(_year_of(None))
        self.assertIsNone(_year_of(""))
        self.assertIsNone(_year_of("未知"))


class DisabledTest(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_returns_none_without_calling_tmdb(self):
        calls = []

        class FakeTmdb:
            async def search_tv(self, name, year=None):
                calls.append(name)
                return []

        r = PosterResolver(FakeTmdb(), enabled=False)
        self.assertIsNone(await r.resolve("超新星.2020.1080p"))
        self.assertEqual(calls, [], "关掉海报后不该再请求 TMDB")

    async def test_unparsable_title_returns_none_without_searching(self):
        calls = []

        class FakeTmdb:
            async def search_tv(self, name, year=None):
                calls.append(name)
                return []

        r = PosterResolver(FakeTmdb(), enabled=True)
        self.assertIsNone(await r.resolve("1080p"))
        self.assertEqual(calls, [], "标题都解析不出片名，不该去搜")

    async def test_caches_negative_results(self):
        """搜不到也要缓存，否则同一部剧会反复打 TMDB。"""
        calls = []

        class FakeTmdb:
            async def search_tv(self, name, year=None):
                calls.append(name)
                return []

        r = PosterResolver(FakeTmdb(), enabled=True)
        await r.resolve("超新星.2020.1080p")
        await r.resolve("超新星.2020.2160p")
        self.assertEqual(len(calls), 1, "同片名同年的重复请求应该命中缓存")

    async def test_caches_by_title_and_year(self):
        calls = []

        class FakeTmdb:
            async def search_tv(self, name, year=None):
                calls.append((name, year))
                return []

        r = PosterResolver(FakeTmdb(), enabled=True)
        await r.resolve("超新星.2020.1080p")
        await r.resolve("超新星.2019.1080p")   # 不同年份 → 应该重新搜
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1], 2020)
        self.assertEqual(calls[1][1], 2019)


class FeedPosterItemsTest(unittest.IsolatedAsyncioTestCase):
    """feed 模式：给条目配海报（每条内容自己的图，而不是一部剧固定一张）。"""

    def _notifier(self):
        from app.notify import Notifier

        class Cfg:
            class telegram:
                send_poster = True
                feed_poster = True

            class tmdb:
                image_base = "https://image.tmdb.org/t/p/w500"

        n = Notifier.__new__(Notifier)
        n.settings = Cfg
        n._poster_cache = {}
        return n

    def _view(self, title, episode=None, size="8.2 GB"):
        from app.notify import ItemView
        from app.release import classify_item, describe_release

        tags = describe_release(title)
        kind, icon = classify_item(title, has_episode=bool(episode))
        return ItemView(
            title=title, download_url="https://pt/dl", size_text=size,
            episode_label=episode, kind=kind, icon=icon, badges=tags.badges(), rank=tags.quality_rank(),
        )

    class _Resolver:
        def __init__(self, data=b"\xff\xd8jpeg"):
            self.data = data
            self.asked: list[str] = []

        async def resolve(self, title):
            self.asked.append(title)
            return self.data

    async def test_builds_one_poster_per_request(self):
        n = self._notifier()
        r = self._Resolver()
        views = [self._view("超新星.2020.1080p.BluRay.x264-GROUP"), self._view("另一部.2019.1080p")]
        items = await n.poster_items(None, views, resolver=r)
        self.assertEqual(len(items), 1, "默认只发第一张，避免刷屏")
        data, caption = items[0]
        self.assertEqual(data, b"\xff\xd8jpeg")
        self.assertIn("超新星", caption)
        self.assertIn("2020", caption)

    async def test_caption_has_size_and_badges(self):
        n = self._notifier()
        views = [self._view("超新星.2020.1080p.BluRay.x264-GROUP", size="8.2 GB")]
        _, caption = (await n.poster_items(None, views, resolver=self._Resolver()))[0]
        self.assertIn("8.2 GB", caption)
        self.assertIn("1080p", caption)

    async def test_no_resolver_returns_empty(self):
        n = self._notifier()
        views = [self._view("超新星.2020.1080p")]
        self.assertEqual(await n.poster_items(None, views, resolver=None), [])

    async def test_no_views_returns_empty(self):
        n = self._notifier()
        self.assertEqual(await n.poster_items(None, [], resolver=self._Resolver()), [])

    async def test_skips_items_without_poster(self):
        """拿不到图的条目直接跳过，不影响其他条目。"""
        n = self._notifier()

        class Sometimes:
            def __init__(self):
                self.n = 0

            async def resolve(self, title):
                self.n += 1
                return None if self.n == 1 else b"\xff\xd8x"

        items = await n.poster_items(None, [self._view("甲.2020.1080p"), self._view("乙.2019.1080p")], resolver=Sometimes(), max_items=2)
        self.assertEqual(len(items), 1)

    async def test_resolver_error_is_swallowed(self):
        """搜海报出错不能把整条推送搞挂。"""
        n = self._notifier()

        class Boom:
            async def resolve(self, title):
                raise RuntimeError("TMDB 挂了")

        items = await n.poster_items(None, [self._view("甲.2020.1080p")], resolver=Boom())
        self.assertEqual(items, [], "出错时应该放弃海报，而不是抛异常")


class PushWithPosterItemsTest(unittest.IsolatedAsyncioTestCase):
    async def _notifier_with_fake_tg(self, photo_ok=True):
        from app.notify import Notifier
        from app.telegram import TgResult

        class Tg:
            enabled = True

            def __init__(self):
                self.calls = []

            async def send_message(self, text, **kw):
                self.calls.append(("text", text))
                return TgResult(True, message_id=1)

            async def send_photo(self, photo, caption="", **kw):
                self.calls.append(("photo", caption))
                if photo_ok:
                    return TgResult(True, message_id=2)
                return TgResult(False, error="bad image")

        class Cfg:
            class telegram:
                send_poster = True
                feed_poster = True

            class tmdb:
                image_base = "https://image.tmdb.org/t/p/w500"

        n = Notifier.__new__(Notifier)
        n.settings = Cfg
        n.tg = Tg()
        n._poster_cache = {}
        return n

    async def test_sends_photo_then_text(self):
        n = await self._notifier_with_fake_tg()
        ok = await n.push("正文清单", poster_items=[(b"\xff\xd8x", "标题")])
        self.assertTrue(ok)
        kinds = [c[0] for c in n.tg.calls]
        self.assertEqual(kinds, ["photo", "text"], "先发图，再发其余条目的文字清单")
        self.assertEqual(n.tg.calls[0][1], "标题")

    async def test_falls_back_to_text_when_photo_fails(self):
        n = await self._notifier_with_fake_tg(photo_ok=False)
        ok = await n.push("正文清单", poster_items=[(b"\xff\xd8x", "标题")])
        self.assertTrue(ok, "发图失败也要保证文字能送达")
        self.assertIn(("text", "正文清单"), n.tg.calls)

    async def test_no_extra_text_when_empty_body(self):
        """只有图、没有正文时不要再发一条空消息。"""
        n = await self._notifier_with_fake_tg()
        ok = await n.push("   ", poster_items=[(b"\xff\xd8x", "标题")])
        self.assertTrue(ok)
        self.assertEqual([c[0] for c in n.tg.calls], ["photo"])


class MessageStructureTest(unittest.TestCase):
    """统一 Message 结构（对齐 MoviePilot）：image 只是 Message 的一个字段。"""

    def test_message_fields_and_caption_fallback(self):
        from app.notify import Message

        m = Message(text="正文", title="标题", image=b"\xff\xd8x")
        self.assertTrue(m.has_image)
        self.assertEqual(m.caption, "标题", "没有显式 caption 时退回 title")

        m2 = Message(text="正文", image=b"\xff\xd8x", image_caption="图下说明")
        self.assertEqual(m2.caption, "图下说明", "显式 image_caption 优先")

        m3 = Message(text="正文")
        self.assertFalse(m3.has_image)


class PushMessageTest(unittest.IsolatedAsyncioTestCase):
    """push_message 投递统一 Message。"""

    async def _notifier_with_fake_tg(self, photo_ok=True):
        from app.notify import Notifier
        from app.telegram import TgResult

        class Tg:
            def __init__(self):
                self.bot_token = "x"
                self.chat_id = "y"
                self.calls = []

            @property
            def enabled(self):
                return bool(self.bot_token and self.chat_id)

            async def send_photo(self, photo, caption="", **kw):
                self.calls.append(("photo", caption))
                if photo_ok:
                    return TgResult(True, message_id=2)
                return TgResult(False, error="bad image")

            async def send_message(self, text, **kw):
                self.calls.append(("text", text))
                return TgResult(True, message_id=1)

        n = Notifier.__new__(Notifier)
        n.settings = None
        n.tg = Tg()
        return n

    async def test_push_message_sends_photo_then_text(self):
        from app.notify import Message

        n = await self._notifier_with_fake_tg()
        ok = await n.push_message(Message(text="正文清单", image=b"\xff\xd8x", image_caption="标题"))
        self.assertTrue(ok)
        kinds = [c[0] for c in n.tg.calls]
        self.assertEqual(kinds, ["photo", "text"])
        self.assertEqual(n.tg.calls[0][1], "标题")

    async def test_push_message_text_only(self):
        from app.notify import Message

        n = await self._notifier_with_fake_tg()
        ok = await n.push_message(Message(text="纯文字"))
        self.assertTrue(ok)
        self.assertEqual([c[0] for c in n.tg.calls], ["text"])


class TitleConsistencyTest(unittest.TestCase):
    """海报 caption 与正文标题必须同源一致（这是本次重构的核心收益）。"""

    def test_clean_title_shared_between_caption_and_card(self):
        from app.notify import ItemView

        # 带未闭合括号残片 + 规格的标题，caption 和卡片标题都要是干净片名
        view = ItemView(title="Affection 2025 1080p BluRay x265 10bit DTS-ADE[疾患 【简英|繁英|简|繁|…")
        self.assertEqual(view.clean_title(), "Affection")

        # 电影标题：clean_title 不应带规格
        mv = ItemView(title="某电影.2024.2160p.UHD.BluRay.Remux.DV.HDR-OurBits")
        self.assertEqual(mv.clean_title(), "某电影")


if __name__ == "__main__":
    unittest.main()
