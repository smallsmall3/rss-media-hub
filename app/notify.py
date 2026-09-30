"""消息排版：把"发现新种 / 入库进度 / 追完退订"渲染成 Telegram 消息。

设计对齐 MoviePilot：统一 `Message` 结构（title / text / image / link），
海报不再是一条独立于正文的旁路 —— 它只是 `Message.image` 字段。
最终渲染（render_html）与投递（push）都只消费这一个对象，天然不会
出现"海报标题"和"正文标题"各拼一份、结果对不上的问题。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

import httpx

from .config import Settings, Subscription
from .db import SubState
from .emby import EmbyClient, LocalSeries
from .reconcile import ReconcileResult, build_progress_bar
from .rss import FeedItem, human_size
from .telegram import TelegramSender, esc

log = logging.getLogger(__name__)

_URL_SECRET_RE = re.compile(
    r"(?:^|[?&])(?:"
    r"passkey|passphrase|torrent_pass|authkey|auth"
    r"|downhash|down_hash|downkey|down_key"
    r"|hash|key|api_key|apikey|secret|token|access_token"
    r"|sid|session|sessionid|phpsessid|uid|userid|user_id"
    r"|c_secure_uid|c_secure_pass|c_secure_login"
    r"|sign|signature|sig|code|verify"
    r")=[^&\s]+",
    re.IGNORECASE,
)


def _url_has_secret(url: str) -> bool:
    """链接里是否带密钥参数（passkey 之类）。"""
    return bool(url) and bool(_URL_SECRET_RE.search(url))

STATUS_ICON = {
    "active": "🟡",
    "done": "✅",
    "removed": "🗑",
    "error": "❌",
}

TMDB_LINK = "https://www.themoviedb.org/tv/{id}"


def local_now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


@dataclass
class Message:
    """一条待投递的推送消息（对齐 MoviePilot 的 Message 结构）。

    字段语义：
      * title：短标题（可选，用于海报 caption 或消息头）
      * text：正文（HTML）
      * image：海报字节（可选）
      * link：主链接（详情页/直链，可选）
      * image_caption：海报图下方配的文字；为空则用 title 兜底
    """

    text: str = ""
    title: str = ""
    image: bytes | None = None
    link: str = ""
    image_caption: str = ""

    @property
    def has_image(self) -> bool:
        return bool(self.image)

    @property
    def caption(self) -> str:
        """海报图的说明文字：优先显式 caption，其次 title。"""
        return self.image_caption or self.title

    def render_html(self) -> str:
        """最终发给 Telegram 的 HTML 正文。"""
        return self.text


@dataclass
class ItemView:
    """推送时需要的条目信息（可直接由 FeedItem 或 DB 行构造）。"""

    title: str
    download_url: str = ""
    detail_url: str = ""    # 详情页（不含 passkey），推送里优先用它
    size_text: str = "-"
    episode_label: str = ""
    published_text: str = ""
    extra: str = ""
    kind: str = ""          # 剧集 / 电影 / 合集 / 音乐 …
    icon: str = "📄"
    badges: list[str] = field(default_factory=list)   # 2160p · HDR · HEVC …
    rank: int = 0           # 规格高低，用于挑"最好的版本"

    def clean_title(self, fallback_limit: int = 60) -> str:
        """干净片名（去掉规格/垃圾残片）。海报 caption 与正文标题共用，
        保证两处标题永远一致（这是对齐 MoviePilot「image 只是 Message 的一个
        字段、从同一数据源渲染」的关键）。"""
        from .titleparse import parse_release_title

        parsed = parse_release_title(self.title or "")
        if parsed.title:
            return parsed.title
        return Notifier.shorten(self.title, fallback_limit)

    @classmethod
    def from_feed(cls, item: FeedItem) -> "ItemView":
        from .release import classify_item, describe_release

        published = ""
        if item.published:
            published = item.published.astimezone().strftime("%m-%d %H:%M")
        tags = describe_release(item.title)
        kind, icon = classify_item(item.title, has_episode=item.episode is not None)
        return cls(
            title=item.title,
            download_url=item.download_url or item.link,
            detail_url=item.detail_url or item.link,
            size_text=item.size_text,
            episode_label=item.episode_label,
            published_text=published,
            kind=kind,
            icon=icon,
            badges=tags.badges(),
            rank=tags.quality_rank(),
        )

    @classmethod
    def from_row(cls, row: dict) -> "ItemView":
        from .release import classify_item, describe_release

        title = row.get("title") or ""
        published = ""
        raw = row.get("published_at") or ""
        if raw:
            published = raw[:16].replace("T", " ")
        episode_label = row.get("episode") or ""
        tags = describe_release(title)
        kind, icon = classify_item(title, has_episode=bool(episode_label))
        return cls(
            title=title,
            download_url=row.get("download_url") or row.get("link") or "",
            size_text=human_size(row.get("size_bytes")),
            episode_label=episode_label,
            published_text=published,
            kind=kind,
            icon=icon,
            badges=tags.badges(),
            rank=tags.quality_rank(),
        )


class Notifier:
    def __init__(self, settings: Settings, sender: TelegramSender, library: EmbyClient) -> None:
        self.settings = settings
        self.tg = sender
        self.library = library
        self._poster_cache: dict[str, bytes | None] = {}
        # 可配置模板（可选覆盖）。None 表示"加载失败/未配置"，退回内置排版。
        self._templates: dict[str, str] | None = None

    # ------------------------------------------------------------------
    # 模板
    # ------------------------------------------------------------------
    def _load_templates(self) -> dict[str, str]:
        """惰性加载 notify_templates.txt。"""
        if getattr(self, "_templates", None) is None:
            from .templates import load_templates

            path = getattr(getattr(self, "settings", None), "notify_templates_file", None)
            self._templates = load_templates(path) if path else {}
        return self._templates

    def _render(self, event: str, context: dict) -> dict[str, str] | None:
        """按事件类型查模板渲染，返回 {title, text, ...} 字典。

        没配模板或渲染失败返回 None（调用方退回内置排版）。
        """
        templates = self._load_templates()
        template = templates.get(event)
        if not template:
            return None
        from .templates import render_dict_template

        try:
            return render_dict_template(template, context)
        except Exception as exc:  # noqa: BLE001
            log.warning("通知模板 %s 渲染失败，退回内置排版：%s", event, exc)
            return None

    def series_title(self, sub: Subscription, result: ReconcileResult | None = None) -> str:
        name = sub.name
        year = sub.year
        if result and result.series:
            name = result.series.name or name
            year = result.series.year or year
        return f"{name}（{year}）" if year else name

    def progress_line(self, result: ReconcileResult) -> str:
        bar = build_progress_bar(result.owned, result.total)
        line = f"{bar} <b>{result.owned}/{result.total}</b> 集"
        if result.total and result.owned < result.total:
            line += f"（{result.percent:.0f}%）"
        return line

    def group_by_episode(
        self, views: list[ItemView]
    ) -> list[tuple[str, list[ItemView]]]:
        """把"同一集的多个版本"归到一组。

        PT 站一集常同时发 2160p/1080p/720p 好几个版本，逐个推会把人烦死。
        分组后每组只占一块，最好的版本突出显示，其余折成一行。
        """
        buckets: dict[str, list[ItemView]] = {}
        order: list[str] = []
        for view in views:
            key = view.episode_label or f"__{view.title}"
            if key not in buckets:
                buckets[key] = []
                order.append(key)
            buckets[key].append(view)
        result: list[tuple[str, list[ItemView]]] = []
        for key in order:
            group = sorted(buckets[key], key=lambda v: (-v.rank, -len(v.badges)))
            result.append(("" if key.startswith("__") else key, group))
        return result

    def render_feed_items(
        self,
        sub: Subscription,
        views: list[ItemView],
        *,
        source: str = "RSS 全量",
    ) -> str:
        """feed 模式：RSS 有什么推什么，不显示入库进度。

        排版目标：**一眼扫完就知道要不要下**。
        所以每条做成一个卡片：图标 + 集号 + 规格标签，其次才是体积和链接；
        同一集的多版本合并成一条，不重复刷屏。
        """
        groups = self.group_by_episode(views)

        # 可配置模板优先（feed_new），命中就不再走内置排版
        item_ctx = [
            {
                "title": v.clean_title(),
                "episode": v.episode_label,
                "size": v.size_text if v.size_text != "-" else "",
                "badges": " · ".join(v.badges),
                "kind": v.kind,
                "icon": v.icon,
                "link": self.link_of(v),
                "link_label": "下载" if self.link_mode == "download" else "查看",
            }
            for v in views
        ]
        rendered = self._render(
            "feed_new",
            {
                "name": sub.name,
                "note": sub.note,
                "source": source,
                "count": len(views),
                "groups": len(groups),
                "items": item_ctx,
                "time": local_now().strftime("%m-%d %H:%M"),
            },
        )
        if rendered is not None:
            return rendered.get("text") or ""

        lines = [f"📡 <b>{esc(sub.name)}</b>"]
        if sub.note:
            lines.append(f"<i>{esc(sub.note)}</i>")

        if len(views) == 1:
            lines.append(f"🆕 <b>新条目</b> · {esc(source)}")
        elif len(groups) < len(views):
            lines.append(f"🆕 <b>{len(groups)} 集 / {len(views)} 个版本</b> · {esc(source)}")
        else:
            lines.append(f"🆕 <b>新条目 ×{len(views)}</b> · {esc(source)}")

        for label, group in groups:
            lines.append("")
            lines.append(self.item_card(label, group, detailed=len(views) == 1))

        lines.append("")
        lines.append(f"🕒 {esc(local_now().strftime('%m-%d %H:%M'))}")
        return "\n".join(lines)

    def item_card(self, label: str, group: list[ItemView], *, detailed: bool = False) -> str:
        """一张条目卡片。group[0] 是规格最高的那个版本，作为主推。"""
        if not group:
            return ""
        top = group[0]
        rows: list[str] = []

        # 有集号就用集号做标题（一眼定位），没有集号就解析出干净片名做标题
        title_shown = False
        if label:
            headline = f"<b>{esc(label)}</b>"
        else:
            headline = f"<b>{esc(top.clean_title())}</b>"
            title_shown = True

        head = f"{top.icon} {headline}"
        if top.size_text and top.size_text != "-":
            head += f" · <b>{esc(top.size_text)}</b>"
        rows.append(head)

        if top.badges:
            rows.append("   " + esc(" · ".join(top.badges)))

        # 标题信息只补一次：有集号且（只有一条 或 集号看不出是哪部剧）时补
        if not title_shown and (detailed or (top.kind in {"电影", "合集", "音乐", "图书", "软件"})):
            rows.append(f"   <i>{esc(self.shorten(top.clean_title(), 78))}</i>")

        link = self.link_of(top)
        if link:
            rows.append(f"   🔗 <a href=\"{esc(link)}\">{'下载' if self.link_mode == 'download' else '查看'}</a>")

        # 其余版本折成一行
        if len(group) > 1:
            others = []
            for view in group[1:4]:
                tag = " ".join(view.badges[:2]) or view.kind or "其他"
                others.append(f"{esc(tag)} {esc(view.size_text)}")
            line = "   ↳ 还有 " + " ｜ ".join(others)
            if len(group) > 4:
                line += f" 等 {len(group) - 1} 个版本"
            rows.append(line)
        return "\n".join(rows)

    @staticmethod
    def shorten(text: str, limit: int) -> str:
        text = (text or "").strip()
        return text if len(text) <= limit else text[: limit - 1] + "…"

    def render_new_items(
        self,
        sub: Subscription,
        views: list[ItemView],
        result: ReconcileResult | None,
        *,
        source: str = "RSS",
    ) -> str:
        groups = self.group_by_episode(views)
        item_ctx = [
            {
                "title": v.clean_title(),
                "episode": v.episode_label,
                "size": v.size_text if v.size_text != "-" else "",
                "badges": " · ".join(v.badges),
                "kind": v.kind,
                "icon": v.icon,
                "link": self.link_of(v),
                "link_label": "下载" if self.link_mode == "download" else "查看",
            }
            for v in views
        ]
        rendered = self._render(
            "show_new",
            {
                "name": self.series_title(sub, result),
                "source": source,
                "count": len(views),
                "groups": len(groups),
                "owned": result.owned if result else 0,
                "total": result.total if result else 0,
                "progress": self.progress_line(result) if result and result.total else "",
                "missing": result.missing_ranges(limit=6) if result and result.missing else "",
                "items": item_ctx,
                "time": local_now().strftime("%Y-%m-%d %H:%M"),
            },
        )
        if rendered is not None:
            return rendered.get("text") or ""

        head = f"🎬 <b>{esc(self.series_title(sub, result))}</b>"
        lines = [head]
        if result and result.total:
            lines.append(self.progress_line(result))
            if result.missing:
                total = len(result.missing)
                lines.append(f"📭 待入库 {total} 集：<code>{esc(result.missing_ranges(limit=6))}</code>")
        else:
            lines.append("📊 集数统计：媒体库暂未找到该剧")

        if len(views) == 1:
            lines.append(f"🆕 <b>发现新资源</b> · {esc(source)}")
        elif len(groups) < len(views):
            lines.append(f"🆕 <b>{len(groups)} 集 / {len(views)} 个版本</b> · {esc(source)}")
        else:
            lines.append(f"🆕 <b>发现 {len(views)} 个新资源</b> · {esc(source)}")

        for label, group in groups:
            lines.append("")
            lines.append(self.item_card(label, group, detailed=len(views) == 1))

        lines.append("")
        lines.append(f"🕒 {esc(local_now().strftime('%Y-%m-%d %H:%M'))}")
        return "\n".join(lines)

    def render_library_update(
        self,
        sub: Subscription,
        result: ReconcileResult,
        codes: list[str],
        items: list[ItemView],
    ) -> str:
        rendered = self._render(
            "library_update",
            {
                "name": self.series_title(sub, result),
                "progress": self.progress_line(result),
                "new_codes": "、".join(codes),
                "missing": result.missing_ranges(),
                "done": not result.missing,
                "time": local_now().strftime("%Y-%m-%d %H:%M"),
            },
        )
        if rendered is not None:
            return rendered.get("text") or ""

        lines = [
            f"📥 <b>{esc(self.series_title(sub, result))}</b> 已入库",
            self.progress_line(result),
        ]
        if codes:
            lines.append(f"✅ 新入库：<code>{esc('、'.join(codes))}</code>")
        if result.missing:
            lines.append(f"📭 待入库：<code>{esc(result.missing_ranges())}</code>")
        else:
            lines.append("🎉 该剧已全部入库")
        if items:
            grouped = self.group_by_episode(items)
            lines.append("")
            lines.append(f"📦 对应资源（{len(grouped)} 条）：")
            for view in items[:8]:
                label = view.episode_label or "-"
                lines.append(f"   • <code>{esc(label)}</code> {esc(self.shorten(view.title, 64))}")
        lines.append("")
        lines.append(f"🕒 {esc(local_now().strftime('%Y-%m-%d %H:%M'))}")
        return "\n".join(lines)

    def render_done(self, sub: Subscription, result: ReconcileResult, *, removed: bool) -> str:
        rendered = self._render(
            "done",
            {
                "name": self.series_title(sub, result),
                "progress": self.progress_line(result),
                "aired": result.aired,
                "owned": result.owned,
                "tmdb_id": result.tmdb_id or "",
                "removed": removed,
                "time": local_now().strftime("%Y-%m-%d %H:%M"),
            },
        )
        if rendered is not None:
            return rendered.get("text") or ""

        lines = [
            "🏁 <b>订阅完成</b>",
            f"🎬 {esc(self.series_title(sub, result))}",
            self.progress_line(result),
            f"📺 已播出 {result.aired} 集 · 库中 {result.owned} 集",
        ]
        if result.tmdb_id:
            lines.append(f"🔗 <a href=\"{TMDB_LINK.format(id=result.tmdb_id)}\">TMDB 条目</a>")
        if removed:
            lines.append("🗑 已自动删除该订阅（remove_when_done = true）")
        else:
            lines.append("📌 订阅已标记完成，等待你手动清理")
        lines.append("")
        lines.append(f"🕒 {esc(local_now().strftime('%Y-%m-%d %H:%M'))}")
        return "\n".join(lines)

    def render_sub_added(self, sub: Subscription, *, title: str = "", year: int | None = None) -> str:
        """「已添加订阅」确认条：风华令 (2026) S01 已添加订阅。

        title/year 来自 TMDB（拿得到就用中文名+年份），拿不到退回订阅名。
        season 只在订阅明确指定了季时显示。
        """
        name = title or sub.name
        season = f"S{sub.season:02d}" if sub.season else ""
        rendered = self._render(
            "sub_added",
            {
                "name": name,
                "title": name,
                "year": year or "",
                "season": season,
            },
        )
        if rendered is not None:
            return rendered.get("text") or ""

        bits = [esc(name)]
        if year:
            bits.append(f"({year})")
        if sub.season:
            bits.append(f"S{sub.season:02d}")
        return " ".join(bits) + " 已添加订阅"

    def render_status(self, states: list[tuple[Subscription, SubState | None]]) -> str:
        lines = ["📋 <b>订阅总览</b>", ""]
        if not states:
            lines.append("（没有订阅）")
            return "\n".join(lines)
        for sub, state in states:
            if state is None:
                lines.append(f"⚪️ <b>{esc(sub.name)}</b> — 尚未巡检")
                continue
            icon = STATUS_ICON.get(state.state, "⚪️")
            lines.append(
                f"{icon} <b>{esc(sub.name)}</b> — {state.owned}/{state.aired or state.total} 集"
                + (f" · 缺 {esc(state.missing)}" if state.missing else " · 完整")
            )
        lines.append("")
        lines.append(f"🕒 {esc(local_now().strftime('%Y-%m-%d %H:%M'))}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------
    async def _poster(self, series_name: str, result: ReconcileResult | None) -> bytes | None:
        if not self.settings.telegram.send_poster:
            return None
        key = str((result.tmdb_id if result else 0) or series_name)
        if key in self._poster_cache:
            return self._poster_cache[key]
        data: bytes | None = None
        local: LocalSeries | None = result.local if result else None
        if local is not None:
            data = await self.library.poster_bytes(local)
        if not data and result and result.series and result.series.poster_path:
            url = self.settings.tmdb.image_base.rstrip("/")
            if "/t/p/" in url:
                url = url.split("/t/p/")[0] + "/t/p/w500"
            try:
                async with httpx.AsyncClient(timeout=20.0) as client:
                    resp = await client.get(f"{url}{result.series.poster_path}")
                    if resp.status_code == 200:
                        data = resp.content
            except Exception as exc:  # noqa: BLE001
                log.debug("下载 TMDB 海报失败：%s", exc)
        self._poster_cache[key] = data
        return data

    @property
    def link_mode(self) -> str:
        """当前链接模式。settings 缺失时退回安全的 detail。"""
        settings = getattr(self, "settings", None)
        telegram = getattr(settings, "telegram", None)
        return getattr(telegram, "link_mode", "detail") or "detail"

    def link_of(self, view: "ItemView") -> str:
        """推送里该用哪个链接。

        默认给**详情页**：直链里带着你的 PT passkey，推送被转发或截图
        就等于把站点通行证交出去了。

        安全兜底：即使配成 download 模式，只要直链里检出密钥、而详情页
        又拿不到，就宁可少一个链接，也不发带密钥的。
        """
        mode = self.link_mode
        detail = view.detail_url or ""
        download = view.download_url or ""
        safe_detail = detail if (detail and not _url_has_secret(detail)) else ""
        if mode == "download" and download and not _url_has_secret(download):
            return download
        if mode == "download" and download and safe_detail:
            # 直链带密钥：不能发出去，退回详情页
            return safe_detail
        if safe_detail:
            return safe_detail
        if download and not _url_has_secret(download):
            return download
        return ""

    async def poster_items(
        self,
        sub: Subscription,
        views: list[ItemView],
        *,
        resolver: Any = None,
        max_items: int = 1,
    ) -> list[tuple[bytes, str]]:
        """给 feed 条目准备「每条内容自己的海报」。

        只取前 max_items 条（默认 1）：一条推送里几十条新种时连发几十张图
        会刷屏并触发 Telegram 限流。其余条目走随后的文字清单。

        caption 只给第一张写完整信息，因为后面的文字清单里有全部条目，
        重复一遍只是噪音。
        """
        if resolver is None or not views:
            return []
        out: list[tuple[bytes, str]] = []
        for view in views[:max_items]:
            matched_name = ""
            try:
                # 优先用 resolve_detail 拿「海报 + TMDB 中文名」；
                # 老 resolver 只有 resolve() 时退回纯字节。
                resolve_detail = getattr(resolver, "resolve_detail", None)
                if resolve_detail:
                    hit = await resolve_detail(view.title)
                    if hit:
                        data, matched_name = hit
                    else:
                        data = None
                else:
                    data = await resolver.resolve(view.title)
            except Exception as exc:  # noqa: BLE001
                log.debug("取海报失败（%s）：%s", view.title, exc)
                continue
            if not data:
                continue
            # caption：feed_new 模板的 image_caption 字段优先，没配退回内置
            caption = self._caption_from_template(view, matched_name) or self._poster_caption(
                view, matched_name=matched_name
            )
            out.append((data, caption))
        return out

    def _caption_from_template(self, view: ItemView, matched_name: str) -> str | None:
        """feed_new 模板里的 image_caption 字段 → 海报图下文字。

        上下文变量（单条条目平铺）：title（TMDB 中文名优先）/ year /
        episode / size / badges / kind / link / sub_name / source。
        没配 image_caption 字段返回 None（用内置 caption）。
        """
        from .titleparse import parse_release_title
        from .templates import parse_template_content, render_with_context

        template = self._load_templates().get("feed_new")
        if not template:
            return None
        try:
            parsed_tpl = parse_template_content(template)
        except Exception:  # noqa: BLE001
            return None
        cap_tpl = parsed_tpl.get("image_caption") if isinstance(parsed_tpl, dict) else None
        if not isinstance(cap_tpl, str) or not cap_tpl.strip():
            return None

        parsed = parse_release_title(view.title)
        name = matched_name or view.clean_title() or view.title
        context = {
            "title": name,
            "name": name,
            "year": parsed.year or "",
            "episode": view.episode_label or "",
            "size": view.size_text if view.size_text != "-" else "",
            "badges": " · ".join(view.badges),
            "kind": view.kind or "",
            "icon": view.icon or "",
            "link": self.link_of(view),
            "source": "RSS 全量",
        }
        try:
            return render_with_context(cap_tpl, context)
        except Exception as exc:  # noqa: BLE001
            log.warning("海报 caption 模板渲染失败，退回内置排版：%s", exc)
            return None

    def _poster_caption(self, view: ItemView, matched_name: str = "") -> str:
        """海报下面的说明文字。优先用 TMDB 中文名（matched_name），
        拿不到才退回标题解析出的片名。"""
        from .titleparse import parse_release_title

        parsed = parse_release_title(view.title)
        lines: list[str] = []
        # 中文名（TMDB zh-CN name）优先，避免推送英文原名（The Girl in Blue → 佳期如梦）
        name = matched_name or view.clean_title() or view.title
        head = f"<b>{esc(name)}</b>"
        if parsed.year:
            head += f"（{parsed.year}）"
        lines.append(head)
        if view.episode_label:
            lines.append(esc(view.episode_label))
        meta = [x for x in (view.size_text, " · ".join(view.badges[:4])) if x and x != "-"]
        if meta:
            lines.append(esc(" · ".join(meta)))
        return "\n".join(lines)

    async def push_message(self, message: Message) -> bool:
        """投递一条统一的 Message（对齐 MoviePilot 的 post_message）。

        有 image 就发图（caption 取 image_caption 或 title），
        正文 text 作为随后的文字消息；没有图就只发文字。
        失败安全：图发不出去就退化为纯文字，绝不因为海报失败丢掉正文。
        """
        if not self.tg.enabled:
            log.warning("Telegram 未配置，跳过推送：\n%s", message.text)
            return False

        photo_ok = False
        if message.image:
            sent = await self.tg.send_photo(message.image, caption=message.caption or "")
            photo_ok = sent.ok
            if not photo_ok:
                log.debug("发海报失败，退化为纯文字：%s", sent.error)

        if photo_ok and not message.text.strip():
            return True
        result = await self.tg.send_message(message.text)
        return photo_ok or result.ok

    async def push(
        self,
        text: str,
        *,
        poster: tuple[str, ReconcileResult | None] | None = None,
        poster_items: list[tuple[bytes, str]] | None = None,
    ) -> bool:
        """发一条推送（兼容旧签名，内部统一走 Message）。

        poster：show 模式的单张海报（按剧，从 Emby/TMDB 取）。
        poster_items：feed 模式的「每条内容自己的海报」，
                      形如 [(图片字节, caption)]。

        feed 模式下**只发第一张图**，其余条目走文字 —— 一条推送几十条新种时，
        连发几十张图会刷屏，还会撞上 Telegram 的限流。
        """
        if not self.tg.enabled:
            log.warning("Telegram 未配置，跳过推送：\n%s", text)
            return False

        if poster_items:
            message = Message(text=text)
            if poster_items[0][0]:
                message.image, message.image_caption = poster_items[0]
            return await self.push_message(message)

        if poster:
            data = await self._poster(poster[0], poster[1])
            message = Message(text=text, image=data)
            return await self.push_message(message)

        return await self.push_message(Message(text=text))
