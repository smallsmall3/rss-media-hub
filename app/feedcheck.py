"""订阅源体检：一次性检查所有配置的 RSS 是否可用，并预览抓到了什么。

为什么需要它：配 RSS 最容易出错（passkey 过期、地址写错、站点要求 UA、
分类参数不对），而原来的反馈渠道只有"等下一轮轮询 + 翻容器日志"。
这个模块把结果直接摆到界面上：

    每个源：能不能抓 / 花了多久 / 多少条 / 最新几条长什么样

也用于"我到底配对了没有"这种自检场景——不需要配 Emby/TMDB。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from .config import Settings, Subscription
from .rss import FeedItem, fetch_feed

log = logging.getLogger(__name__)

PREVIEW_LIMIT = 5  # 每个源预览几条


@dataclass
class FeedPreview:
    """预览的一条条目。"""

    title: str
    episode: str = ""
    size: str = "-"
    published: str = ""
    badges: list[str] = field(default_factory=list)
    has_download: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "episode": self.episode,
            "size": self.size,
            "published": self.published,
            "badges": self.badges,
            "has_download": self.has_download,
        }


@dataclass
class FeedCheck:
    """一个 RSS 地址的体检结果。"""

    sub_id: str
    sub_name: str
    url: str
    mode: str = "feed"
    ok: bool = False
    error: str = ""
    elapsed: float = 0.0
    item_count: int = 0
    newest: str = ""
    previews: list[FeedPreview] = field(default_factory=list)

    @property
    def masked_url(self) -> str:
        """展示用地址：把 passkey 之类的密钥打码。"""
        import re

        return re.sub(
            r"((?:passkey|passphrase|torrent_pass|api_key|apikey|authkey|key)=)([^&\s]+)",
            r"\1***",
            self.url,
            flags=re.IGNORECASE,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "sub_id": self.sub_id,
            "sub_name": self.sub_name,
            "url": self.masked_url,
            "mode": self.mode,
            "ok": self.ok,
            "error": self.error,
            "elapsed": round(self.elapsed, 2),
            "item_count": self.item_count,
            "newest": self.newest,
            "previews": [p.to_dict() for p in self.previews],
        }


@dataclass
class FeedsReport:
    checks: list[FeedCheck] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    skipped_disabled: int = 0

    @property
    def elapsed(self) -> float:
        return max(0.0, (self.finished_at or time.time()) - self.started_at)

    @property
    def ok_count(self) -> int:
        return sum(1 for c in self.checks if c.ok)

    @property
    def failed(self) -> list[FeedCheck]:
        return [c for c in self.checks if not c.ok]

    @property
    def healthy(self) -> bool:
        return bool(self.checks) and not self.failed

    def render_text(self) -> str:
        lines = ["=" * 78, "订阅源体检报告", "=" * 78]
        if not self.checks:
            lines.append("没有任何启用的 RSS 源（检查 subscriptions.yaml）")
            if self.skipped_disabled:
                lines.append(f"（有 {self.skipped_disabled} 条订阅处于停用状态）")
            return "\n".join(lines) + "\n"

        lines.append(
            f"共 {len(self.checks)} 个源：✅ 正常 {self.ok_count} 个"
            + (f"，❌ 失败 {len(self.failed)} 个" if self.failed else "")
            + f"，耗时 {self.elapsed:.1f}s"
        )
        if self.skipped_disabled:
            lines.append(f"（已跳过 {self.skipped_disabled} 条停用的订阅）")
        lines.append("")

        for check in self.checks:
            mark = "✅" if check.ok else "❌"
            lines.append(f"{mark} [{check.sub_name}] {check.masked_url}")
            if not check.ok:
                lines.append(f"     {check.error}")
                continue
            lines.append(
                f"     {check.item_count} 条 · {check.elapsed:.2f}s"
                + (f" · 最新 {check.newest}" if check.newest else "")
            )
            for preview in check.previews:
                bits = [preview.episode] if preview.episode else []
                bits.append(preview.size)
                if preview.badges:
                    bits.append("·".join(preview.badges[:3]))
                meta = "  ".join(b for b in bits if b and b != "-")
                lines.append(f"       {meta:<34} {preview.title[:52]}")
            lines.append("")

        if self.failed:
            lines.append("---- 排错建议 ----")
            lines.append("  · 401/403：passkey 过期或地址不完整，去站点重新复制 RSS 地址")
            lines.append("  · 404：地址路径写错，或该分类已被站点移除")
            lines.append("  · 超时/连接失败：NAS 访问不了该站点，需要配 RMH_PROXY 走代理")
            lines.append("  · 返回 HTML 而不是 XML：多半被 CF 盾拦了，或需要登录态")
            lines.append("")

        return "\n".join(lines).rstrip() + "\n"

    def render_json(self, *, indent: int = 2) -> str:
        return json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "elapsed_seconds": round(self.elapsed, 2),
                "total": len(self.checks),
                "ok": self.ok_count,
                "failed": len(self.failed),
                "skipped_disabled": self.skipped_disabled,
                "checks": [c.to_dict() for c in self.checks],
            },
            ensure_ascii=False,
            indent=indent,
        )


def build_preview(item: FeedItem, *, limit_title: int = 90) -> FeedPreview:
    from .release import describe_release

    published = ""
    if item.published:
        published = item.published.astimezone().strftime("%m-%d %H:%M")
    title = item.title or ""
    if len(title) > limit_title:
        title = title[: limit_title - 1] + "…"
    return FeedPreview(
        title=title,
        episode=item.episode_label,
        size=item.size_text,
        published=published,
        badges=describe_release(item.title).badges(),
        has_download=bool(item.download_url),
    )


def collect_sources(
    subs: Iterable[Subscription],
) -> tuple[list[tuple[Subscription, str]], int]:
    """把订阅摊平成 (订阅, 地址) 列表；同一个地址只留一份。返回 (列表, 被跳过的停用订阅数)。"""
    sources: list[tuple[Subscription, str]] = []
    seen: set[str] = set()
    skipped = 0
    for sub in subs:
        if not sub.enabled:
            skipped += 1
            continue
        for url in sub.rss_urls:
            if url in seen:
                continue
            seen.add(url)
            sources.append((sub, url))
    return sources, skipped


async def check_feeds(
    settings: Settings,
    http: Any,
    *,
    concurrency: int = 4,
    preview_limit: int = PREVIEW_LIMIT,
    subs: Iterable[Subscription] | None = None,
    timeout: float = 15.0,
    retries: int = 1,
) -> FeedsReport:
    """并发检查所有 RSS 源。单个源失败不影响其他源。

    默认参数刻意比轮询更"急"：体检是给人当场看的，不应该等几分钟。
    所以超时 15 秒、只试 1 次（失败就是失败，报出来比耗着有用）。
    """
    report = FeedsReport()
    sources, skipped = collect_sources(subs if subs is not None else settings.subscriptions)
    report.skipped_disabled = skipped
    if not sources:
        report.finished_at = time.time()
        return report

    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(sub: Subscription, url: str) -> FeedCheck:
        check = FeedCheck(sub_id=sub.id, sub_name=sub.name, url=url, mode=sub.mode)
        started = time.time()
        async with sem:
            try:
                items = await fetch_feed(http, url, retries=max(1, retries), timeout=timeout)
                check.ok = True
                check.item_count = len(items)
                # 按发布时间倒序取最新的几条做预览
                ordered = sorted(
                    items,
                    key=lambda it: it.published.timestamp() if it.published else 0.0,
                    reverse=True,
                )
                check.previews = [build_preview(it) for it in ordered[:preview_limit]]
                if ordered and ordered[0].published:
                    check.newest = ordered[0].published.astimezone().strftime("%m-%d %H:%M")
                elif ordered:
                    check.newest = "未知时间"
            except Exception as exc:  # noqa: BLE001
                check.ok = False
                check.error = _explain_error(exc)
                log.info("[%s] 体检抓取失败：%s", sub.name, exc)
        check.elapsed = time.time() - started
        return check

    results = await asyncio.gather(*(one(sub, url) for sub, url in sources), return_exceptions=True)
    for entry in results:
        if isinstance(entry, FeedCheck):
            report.checks.append(entry)
        elif isinstance(entry, BaseException):
            report.checks.append(
                FeedCheck(sub_id="", sub_name="?", url="", ok=False, error=_explain_error(entry))
            )
    report.finished_at = time.time()
    return report


def _explain_error(exc: BaseException) -> str:
    """把底层异常翻译成"该怎么办"。"""
    text = f"{type(exc).__name__}: {exc}".strip(": ")
    low = text.lower()
    if "401" in low or "403" in low or "unauthorized" in low or "forbidden" in low:
        return f"{text} —— 鉴权失败：passkey 可能已过期，去站点重新复制 RSS 地址"
    if "404" in low:
        return f"{text} —— 地址不存在：检查路径与分类参数是否写错"
    if "timeout" in low or "timed out" in low:
        return f"{text} —— 超时：NAS 可能访问不了该站点，试试配 RMH_PROXY"
    if "name or service not known" in low or "getaddrinfo" in low or "nodename" in low:
        return f"{text} —— 域名解析失败：检查地址拼写或 DNS"
    if "ssl" in low or "certificate" in low:
        return f"{text} —— 证书问题：站点证书异常，或需要跳过校验"
    return text
