"""通知模板：可配置推送文案（对齐 MoviePilot 的 Jinja2 模板机制）。

模板格式与 MoviePilot 一致——一段 **Python 字典字面量**，字段值用
**Jinja2 模板**渲染：

    {
        "title": "🎬 {{title}}{% if year %}（{{year}}）{% endif %}",
        "text": "{% if season_episode %}📺 {{season_episode}}{% endif %}…"
    }

渲染流程（与 MoviePilot 的 TemplateHelper.render 同款）：
  1. ``ast.literal_eval`` 把模板字符串解析成字典
  2. 对每个字符串字段用 Jinja2 渲染（传入统一上下文变量）
  3. 渲染结果写回 Message 的 title / text 字段

变量对齐 MoviePilot 的 TemplateContextBuilder 常用字段：
  title / name / year / season_episode / season / episode / size /
  badges / poster / link / kind / source / count / owned / total …
（本工具只提供它有意义的子集，见 build_context。）

设计原则：模板是"可选覆盖"。某类事件没配模板 → 退回内置排版。
"""

from __future__ import annotations

import ast
import json
import logging
import re
from pathlib import Path
from typing import Any

from jinja2 import Template

log = logging.getLogger(__name__)


def render_dict_template(template_content: str, context: dict[str, Any]) -> dict[str, str]:
    """渲染一段 MoviePilot 风格的字典模板，返回 {字段: 渲染结果}。

    只返回模板里显式出现的字段（title/text/image/link 等），
    调用方决定哪些字段写回 Message。
    """
    parsed = parse_template_content(template_content)
    if not isinstance(parsed, dict):
        raise ValueError("模板解析结果必须是字典")

    out: dict[str, str] = {}
    for key, value in parsed.items():
        if isinstance(value, str):
            out[str(key)] = render_with_context(value, context)
        else:
            out[str(key)] = value
    return out


def parse_template_content(template_content: str) -> Any:
    """把模板字符串解析成结构（优先 ast.literal_eval，退 JSON）。"""
    content = (template_content or "").strip()
    if not content:
        raise ValueError("模板为空")
    try:
        parsed = ast.literal_eval(content)
        if isinstance(parsed, dict):
            return parsed
    except (ValueError, SyntaxError):
        pass
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    raise ValueError("模板必须是字典字面量（形如 {\"title\": \"...\"}）")


def render_with_context(template_content: str, context: dict[str, Any]) -> str:
    """用 Jinja2 渲染一段字符串模板。渲染失败退回原串（宁缺勿错）。"""
    try:
        return Template(template_content).render(**context)
    except Exception as exc:  # noqa: BLE001
        log.debug("Jinja2 渲染失败，退回原串：%s", exc)
        return template_content


def load_templates(path: Path | None) -> dict[str, str]:
    """读取通知模板文件，返回 {事件类型: 模板文本}。

    用 ``=== 事件名 ===`` 分段，段内是 MoviePilot 风格的字典模板：
        === feed_new ===
        {"title": "📡 {{name}}", "text": "{{#...}}"}
        === sub_added ===
        {"title": "🎉 {{title}} 已添加订阅"}

    文件不存在或解析失败都返回空字典（模板是可选覆盖）。
    """
    if not path or not path.exists():
        return {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log.warning("通知模板读取失败，退回内置排版：%s", exc)
        return {}

    out: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for raw in text.splitlines():
        m = re.match(r"^\s*===\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*===\s*$", raw)
        if m:
            if current is not None:
                out[current] = "\n".join(buf).strip()
            current = m.group(1)
            buf = []
            continue
        if current is not None:
            buf.append(raw)
    if current is not None:
        out[current] = "\n".join(buf).strip()

    return {k: v for k, v in out.items() if v}


def save_templates(path: Path | None, templates: dict[str, str]) -> None:
    """把 {事件名: 模板文本} 写回 ``=== 事件名 ===`` 分隔格式。

    空值（None / 空串）的条目会被跳过（即"删除该模板"）。
    """
    if not path:
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for event, text in templates.items():
        if not text or not str(text).strip():
            continue
        lines.append(f"=== {event} ===")
        lines.append(str(text).strip())
        lines.append("")
    Path(path).write_text("\n".join(lines).strip() + ("\n" if lines else ""), encoding="utf-8")
