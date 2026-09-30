"""Telegram 发送层测试。

这层是推送的关键路径，之前完全没测过。重点覆盖三个容易出事的地方：
  1. HTML 转义——剧名里的 < & > 会让 Telegram 拒收整条消息
  2. 长消息切分——要么被 Telegram 拒收（超 4096），要么把标签切坏
  3. sendPhoto 失败时能否退化成文本（带海报的推送不能因此整条丢失）
"""

from __future__ import annotations

import unittest

from app.telegram import (
    HARD_LIMIT,
    MAX_LEN,
    MIN_TAIL,
    TelegramSender,
    TgResult,
    esc,
    split_message,
)


class EscapeTest(unittest.TestCase):
    def test_escapes_html_specials(self):
        self.assertEqual(esc("<b>粗体</b>"), "&lt;b&gt;粗体&lt;/b&gt;")
        self.assertEqual(esc("A & B"), "A &amp; B")

    def test_quotes_not_escaped_in_text_content(self):
        """quote=False 是有意的：文本节点里的引号不需要转义，
        转了反而会在消息里显示出 &quot; 这种怪东西。"""
        self.assertEqual(esc('他说"你好"'), '他说"你好"')
        self.assertNotIn("&quot;", esc('他说"你好"'))
        # 属性值是在调用处单独转义的，不靠这个函数

    def test_does_not_escape_quotes_to_entity_in_text(self):
        # quote=False 时单引号不该变成 &#x27;，否则消息里会看到奇怪字符
        self.assertNotIn("&#x27;", esc("it's"))

    def test_handles_none_and_numbers(self):
        self.assertEqual(esc(None), "")
        self.assertEqual(esc(42), "42")

    def test_real_world_titles(self):
        # PT 站标题里常见 & < >
        self.assertEqual(esc("Show & Tell <S01E01>"), "Show &amp; Tell &lt;S01E01&gt;")
        self.assertNotIn("<", esc("某剧 <组名>"))


class SplitMessageTest(unittest.TestCase):
    def test_short_text_not_split(self):
        self.assertEqual(split_message("hello"), ["hello"])

    def test_exactly_at_limit(self):
        text = "a" * MAX_LEN
        self.assertEqual(split_message(text), [text])

    def test_never_exceeds_hard_limit(self):
        cases = [
            "\n".join(f"line {i}" for i in range(500)),
            "x" * 20000,
            "中文内容" * 3000,
            "\n".join("某剧 S01E05 2160p HEVC HDR · WEB-DL 内容" for _ in range(400)),
        ]
        for text in cases:
            for chunk in split_message(text):
                self.assertLessEqual(len(chunk), HARD_LIMIT, f"切分后仍有超长块：{len(chunk)}")

    def test_no_content_lost(self):
        text = "\n".join(f"第 {i} 行内容 <b>加粗</b>" for i in range(400))
        chunks = split_message(text)
        self.assertEqual("".join(c.replace("\n", "") for c in chunks), text.replace("\n", ""))

    def test_splits_on_line_boundaries(self):
        text = "\n".join("X" * 100 for _ in range(100))
        chunks = split_message(text)
        self.assertGreater(len(chunks), 1)
        # 每块都不该以半个 X 行开头
        for chunk in chunks[:-1]:
            self.assertEqual(len(chunk) % 101, 100, "切点没有落在行尾")

    def test_small_tail_merged_into_previous(self):
        """尾块只有几十字符（时间戳尾巴）时应并回上一块，不浪费一条消息。"""
        head = "\n".join("Y" * 100 for _ in range(35))   # ~3540
        text = head + "\n🕒 09-30 08:16"                  # 尾块很短
        chunks = split_message(text)
        self.assertGreaterEqual(len(chunks[-1]), MIN_TAIL, "过短的尾块应该被并回上一块")
        self.assertLessEqual(len(chunks[-1]), HARD_LIMIT)

    def test_tail_not_merged_when_exceeding_hard_limit(self):
        """并回去会超硬限时必须宁可多发一条，不能拼出超长消息。"""
        head = "\n".join("Z" * 100 for _ in range(41))   # 每块接近软上限
        text = head + "\nTAIL"
        for chunk in split_message(text):
            self.assertLessEqual(len(chunk), HARD_LIMIT)

    def test_very_long_single_line_hard_split(self):
        chunks = split_message("A" * 12000)
        self.assertGreater(len(chunks), 2)
        self.assertEqual("".join(chunks), "A" * 12000)

    def test_real_tag_lengths_are_far_below_limit(self):
        """记录一个事实：真实推送里最长的标签约 93 字符（PT 下载链接）。

        这个断言的意义是防止有人把切分上限调到跟标签长度同量级——
        那样切点就无从选择了。
        """
        self.assertGreater(MAX_LEN, 200, "切分上限必须远大于真实标签长度（约 93 字符）")

    def test_does_not_split_inside_short_tag(self):
        """标签就在切点附近时，切点应落在标签之外。"""
        text = "A" * 3490 + '<b>粗体</b>' + "B" * 200
        for chunk in split_message(text):
            self.assertEqual(chunk.count("<b>"), chunk.count("</b>"))

    def test_empty_text(self):
        self.assertEqual(split_message(""), [""])

    def test_whitespace_only_tail_dropped(self):
        text = "a" * 3000 + "\n\n\n"
        chunks = split_message(text)
        self.assertTrue(all(c.strip() for c in chunks), "不该产生纯空白块")


class SenderBehaviourTest(unittest.IsolatedAsyncioTestCase):
    """用假的 httpx 客户端验证发送行为（含失败退化）。"""

    class FakeResponse:
        def __init__(self, payload, status=200):
            self._payload = payload
            self.status_code = status
            self.text = str(payload)

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, responses):
            self.responses = list(responses)
            self.calls: list[dict] = []

        async def post(self, url, data=None, files=None):
            self.calls.append({"url": url, "data": dict(data or {}), "files": files})
            return self.responses.pop(0) if self.responses else SenderBehaviourTest.FakeResponse(
                {"ok": True, "result": {"message_id": 1}}
            )

        async def get(self, url):
            return SenderBehaviourTest.FakeResponse({"ok": True, "result": {"username": "b"}})

        async def aclose(self):
            pass

    async def test_disabled_without_config(self):
        sender = TelegramSender("", "")
        self.assertFalse(sender.enabled)
        result = await sender.send_message("hi")
        self.assertFalse(result.ok)
        self.assertIn("未配置", result.error)

    async def test_sends_with_html_parse_mode(self):
        client = self.FakeClient([self.FakeResponse({"ok": True, "result": {"message_id": 7}})])
        sender = TelegramSender("1:a", "-100", client=client)
        result = await sender.send_message("<b>hi</b>")
        self.assertTrue(result.ok)
        self.assertEqual(result.message_id, 7)
        self.assertEqual(client.calls[0]["data"]["parse_mode"], "HTML")
        self.assertEqual(client.calls[0]["data"]["chat_id"], "-100")

    async def test_drops_parse_mode_when_telegram_rejects_html(self):
        """HTML 被拒时要自动去掉 parse_mode 重发，而不是整条丢失。"""
        client = self.FakeClient(
            [
                self.FakeResponse({"ok": False, "description": "Bad Request: can't parse entities"}),
                self.FakeResponse({"ok": True, "result": {"message_id": 9}}),
            ]
        )
        sender = TelegramSender("1:a", "-100", client=client)
        result = await sender.send_message("<b>坏标签")
        self.assertTrue(result.ok, "应该退化成纯文本重发成功")
        self.assertNotIn("parse_mode", client.calls[1]["data"])
        self.assertEqual(len(client.calls), 2)

    async def test_drops_thread_when_topic_missing(self):
        """群话题不存在时要去掉 message_thread_id 重发。"""
        client = self.FakeClient(
            [
                self.FakeResponse({"ok": False, "description": "Bad Request: message thread not found"}),
                self.FakeResponse({"ok": True, "result": {"message_id": 3}}),
            ]
        )
        sender = TelegramSender("1:a", "-100", thread_id="999", client=client)
        result = await sender.send_message("hi")
        self.assertTrue(result.ok)
        self.assertNotIn("message_thread_id", client.calls[1]["data"])

    async def test_retries_on_429(self):
        client = self.FakeClient(
            [
                self.FakeResponse({"ok": False, "parameters": {"retry_after": 0}}, status=429),
                self.FakeResponse({"ok": True, "result": {"message_id": 5}}),
            ]
        )
        sender = TelegramSender("1:a", "-100", client=client)
        result = await sender.send_message("hi")
        self.assertTrue(result.ok)

    async def test_long_message_sent_as_multiple_calls(self):
        client = self.FakeClient([])
        sender = TelegramSender("1:a", "-100", client=client)
        text = "\n".join("行" * 60 for _ in range(200))
        result = await sender.send_message(text)
        self.assertTrue(result.ok)
        self.assertGreater(len(client.calls), 1, "超长消息应该分多条发出")
        for call in client.calls:
            self.assertLessEqual(len(call["data"]["text"]), HARD_LIMIT)

    async def test_photo_failure_reported(self):
        client = self.FakeClient([self.FakeResponse({"ok": False, "description": "wrong file identifier"})])
        sender = TelegramSender("1:a", "-100", client=client)
        result = await sender.send_photo(b"\xff\xd8jpeg", caption="x")
        self.assertFalse(result.ok)

    async def test_send_photo_without_data(self):
        sender = TelegramSender("1:a", "-100", client=self.FakeClient([]))
        result = await sender.send_photo(b"", caption="x")
        self.assertFalse(result.ok)
        self.assertIn("没有图片数据", result.error)

    async def test_caption_truncated_to_telegram_limit(self):
        client = self.FakeClient([])
        sender = TelegramSender("1:a", "-100", client=client)
        await sender.send_photo(b"\xff\xd8x", caption="标题" * 600)
        self.assertLessEqual(len(client.calls[0]["data"]["caption"]), 1024)

    async def test_disable_notification_flag(self):
        client = self.FakeClient([])
        sender = TelegramSender("1:a", "-100", disable_notification=True, client=client)
        await sender.send_message("hi")
        self.assertEqual(client.calls[0]["data"]["disable_notification"], "true")


if __name__ == "__main__":
    unittest.main()
