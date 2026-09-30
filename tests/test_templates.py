"""通知模板引擎测试（可配置推送文案，对齐 MoviePilot Jinja2 模板）。"""

from __future__ import annotations

import contextlib
import itertools
import os
import shutil
import unittest
from pathlib import Path

from app.templates import load_templates, parse_template_content, render_dict_template, render_with_context

_counter = itertools.count()


@contextlib.contextmanager
def temp_dir():
    base = Path(os.environ.get("RMH_TEST_TMP") or Path(__file__).resolve().parent / ".tmp")
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"tpl-{os.getpid()}-{next(_counter)}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class ParseTemplateTest(unittest.TestCase):
    def test_dict_literal(self):
        self.assertEqual(parse_template_content('{"title": "x", "text": "y"}'), {"title": "x", "text": "y"})

    def test_single_quote_dict(self):
        self.assertEqual(parse_template_content("{'title': 'x'}"), {"title": "x"})

    def test_json(self):
        self.assertEqual(parse_template_content('{"title": "x"}'), {"title": "x"})

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            parse_template_content("不是字典")


class RenderDictTemplateTest(unittest.TestCase):
    def test_jinja_vars(self):
        tpl = '{"title": "🎬 {{title}}", "text": "{{title}} ({{year}})"}'
        out = render_dict_template(tpl, {"title": "风华令", "year": 2026})
        self.assertEqual(out, {"title": "🎬 风华令", "text": "风华令 (2026)"})

    def test_jinja_if(self):
        tpl = '{"text": "{{title}}{% if year %}（{{year}}）{% endif %}"}'
        out = render_dict_template(tpl, {"title": "某剧", "year": 2024})
        self.assertEqual(out["text"], "某剧（2024）")
        out2 = render_dict_template(tpl, {"title": "某剧", "year": ""})
        self.assertEqual(out2["text"], "某剧")

    def test_missing_var_is_empty(self):
        out = render_dict_template('{"text": "[{{missing}}]"}', {})
        self.assertEqual(out["text"], "[]")

    def test_html_not_escaped(self):
        out = render_dict_template('{"text": "<b>{{title}}</b>"}', {"title": "X"})
        self.assertEqual(out["text"], "<b>X</b>")

    def test_jinja_error_falls_back(self):
        """模板语法错误不该让整个推送崩溃。"""
        out = render_dict_template('{"text": "{% if %}broken"}', {"title": "X"})
        self.assertEqual(out["text"], "{% if %}broken")


class LoadTemplatesTest(unittest.TestCase):
    def test_load_txt(self):
        with temp_dir() as root:
            p = root / "notify_templates.txt"
            p.write_text(
                "=== feed_new ===\n"
                '{"title": "📡 {{name}}", "text": "{{name}} ×{{count}}"}\n'
                "=== sub_added ===\n"
                '{"text": "🎉 {{title}} 已添加订阅"}\n'
                "=== empty_one ===\n"
                "\n",
                encoding="utf-8",
            )
            tpls = load_templates(p)
            self.assertIn("feed_new", tpls)
            self.assertIn("sub_added", tpls)
            self.assertNotIn("empty_one", tpls, "空模板不算配置")

    def test_missing_file(self):
        with temp_dir() as root:
            self.assertEqual(load_templates(root / "nope.txt"), {})


class NotifierTemplateOverrideTest(unittest.TestCase):
    """Notifier 在有模板时走模板、没模板时退回内置排版。"""

    def _notifier_with_template(self, event: str, tpl: str):
        from app.notify import Notifier

        n = Notifier.__new__(Notifier)
        n._templates = {event: tpl}
        n.settings = None
        return n

    def test_feed_new_template_used(self):
        from app.notify import ItemView
        from app.config import Subscription

        n = self._notifier_with_template("feed_new", '{"text": "自定义：{{name}} ×{{count}}"}')
        sub = Subscription(id="t", name="源", mode="feed", rss="https://x/rss")
        views = [ItemView(title="某剧.2024.1080p")]
        text = n.render_feed_items(sub, views)
        self.assertEqual(text, "自定义：源 ×1")

    def test_no_template_falls_back_to_builtin(self):
        from app.notify import ItemView
        from app.config import Subscription

        n = self._notifier_with_template("other_event", '{"text": "xxx"}')
        sub = Subscription(id="t", name="源", mode="feed", rss="https://x/rss")
        views = [ItemView(title="某剧 S01E01 1080p", episode_label="S01E01")]
        text = n.render_feed_items(sub, views)
        self.assertIn("源", text)
        self.assertIn("S01E01", text, "没配 feed_new 模板时走内置排版")

    def test_sub_added_template_used(self):
        from app.config import Subscription

        n = self._notifier_with_template("sub_added", '{"text": "🎉 {{title}} ({{year}}) {{season}}"}')
        sub = Subscription(id="t", name="风华令", season=1)
        text = n.render_sub_added(sub, title="风华令", year=2026)
        self.assertEqual(text, "🎉 风华令 (2026) S01")


if __name__ == "__main__":
    unittest.main()
