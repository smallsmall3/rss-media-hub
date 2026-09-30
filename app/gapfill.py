"""查漏：把「媒体库缺的集」和「RSS 里现成的条目」对上。

这是"以媒体库为基准反向订阅"的落地点：

    Emby 全库扫描 ──► 每部剧缺哪些集（S04E84-S04E87）
                            │
                            ▼
                    当前 RSS 里有这些集吗？
                            │
                    ┌───────┴────────┐
                    ▼                ▼
              有 → 推给你下载      没有 → 标成"RSS 中暂未出现"

⚠️ 必须清楚的**能力边界**：
    RSS 只包含站点最近的一批更新（通常几十条），不是全站索引。
    所以这里只能发现"缺的集**恰好还在 RSS 窗口里**"的情况；
    已经翻页过去的旧集，靠读 RSS 是永远找不到的——那需要站点的搜索接口。
    本模块刻意只做"被动查漏"，不假装能搜全网。
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from .config import Settings, Subscription
from .libraryscan import STATUS_PARTIAL, SeriesScan, ScanResult
from .rss import FeedItem, fetch_feed

log = logging.getLogger(__name__)


def parse_code(code: str) -> tuple[int | None, int | None]:
    """把 S01E02 这种集号解析成 (季, 集)。解析不了返回 (None, None)。"""
    match = re.fullmatch(r"[Ss](\d{1,3})[Ee](\d{1,4})", (code or "").strip())
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def item_matches_code(item: FeedItem, season: int | None, episode: int) -> bool:
    """判断一条 RSS 条目是不是某一集。

    宽松匹配是有意的：字幕组命名千奇百怪，宁可多报也不能漏报，
    最终由人决定下不下。所以只要集号对得上就算命中。
    """
    if item.episode is None:
        return False
    if item.season is not None and season is not None:
        # 两边都有季号时，季必须一致
        if item.season != season:
            return False
        return item.episode == episode
    # 只有一边有季号，或以绝对集数命名 → 只比集号
    return item.episode == episode


@dataclass
class GapMatch:
    """一个缺集，以及它在当前 RSS 里能找到的资源。"""

    season: int
    episode: int
    code: str
    name: str = ""
    air_date: Any = None
    items: list[FeedItem] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return bool(self.items)

    @property
    def best(self) -> FeedItem | None:
        if not self.items:
            return None
        # 优先带直链（PT 站 enclosure 带 passkey，可直接下）、其次规格高、再次体积大
        from .release import describe_release

        return max(
            self.items,
            key=lambda it: (
                bool(it.download_url),
                describe_release(it.title).quality_rank(),
                it.size_bytes or 0,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        best = self.best
        return {
            "season": self.season,
            "episode": self.episode,
            "code": self.code,
            "name": self.name,
            "available": self.available,
            "candidates": len(self.items),
            "best": None
            if best is None
            else {
                "title": best.title,
                "size": best.size_text,
                "badges": _badges_of(best),
                "download_url": best.download_url or best.link,
                "published": best.published.isoformat() if best.published else "",
            },
        }


def _badges_of(item: FeedItem) -> list[str]:
    from .release import describe_release

    return describe_release(item.title).badges()


@dataclass
class SeriesGap:
    """一部剧的查漏结果。"""

    name: str
    display_name: str
    tmdb_id: int | None
    emby_id: str
    owned: int
    total: int
    gaps: list[GapMatch] = field(default_factory=list)

    @property
    def available_gaps(self) -> list[GapMatch]:
        return [g for g in self.gaps if g.available]

    @property
    def missing_gaps(self) -> list[GapMatch]:
        return [g for g in self.gaps if not g.available]

    @property
    def covered(self) -> int:
        return len(self.available_gaps)

    @property
    def missing_count(self) -> int:
        return len(self.gaps)

    def to_dict(self, *, item_limit: int = 3) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "tmdb_id": self.tmdb_id,
            "emby_id": self.emby_id,
            "owned": self.owned,
            "total": self.total,
            "missing_count": self.missing_count,
            "covered": self.covered,
            "gaps": [g.to_dict() for g in self.gaps[: max(1, item_limit)]],
            "gaps_total": len(self.gaps),
        }


@dataclass
class GapReport:
    """一次查漏的汇总。"""

    feeds_ok: int = 0
    feeds_failed: int = 0
    feed_items: int = 0
    scanned_series: int = 0
    series_with_gaps: int = 0
    checks: list[SeriesGap] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0

    @property
    def elapsed(self) -> float:
        return max(0.0, (self.finished_at or time.time()) - self.started_at)

    @property
    def total_missing(self) -> int:
        return sum(s.missing_count for s in self.checks)

    @property
    def total_covered(self) -> int:
        return sum(s.covered for s in self.checks)

    def with_available(self) -> list[SeriesGap]:
        return [s for s in self.checks if s.available_gaps]

    def render_text(self, *, top: int = 20) -> str:
        lines = ["=" * 78, "查漏报告（媒体库缺的集 × 当前 RSS 里现成的资源）", "=" * 78]
        lines.append(
            f"扫了 {self.scanned_series} 部有缺集的剧，共缺 {self.total_missing} 集；"
            f"其中 {self.total_covered} 集当前能在 RSS 里找到"
        )
        lines.append(
            f"RSS：{self.feeds_ok} 个源成功 / {self.feeds_failed} 个失败，共 {self.feed_items} 条条目，"
            f"耗时 {self.elapsed:.1f}s"
        )
        lines.append("")
        lines.append("⚠️ 只能发现「缺的集还在 RSS 窗口里」的情况；已翻页的旧集需要站点搜索接口才能找。")
        lines.append("")

        ready = sorted(self.with_available(), key=lambda s: -s.covered)
        if ready:
            lines.append(f"---- 有货可下的 {len(ready)} 部 ----")
            for series in ready[:top]:
                lines.append(f"  {series.display_name}  缺 {series.missing_count} 集，现成 {series.covered} 集")
                for gap in series.available_gaps[:5]:
                    best = gap.best
                    assert best is not None
                    badges = " · ".join(_badges_of(best)[:3])
                    lines.append(
                        f"      {gap.code}  {best.size_text:>9}  {badges:<28} {best.title[:44]}"
                    )
                if series.covered > 5:
                    lines.append(f"      …还有 {series.covered - 5} 集")
            lines.append("")

        waiting = [s for s in self.checks if not s.available_gaps]
        if waiting:
            lines.append(f"---- 暂时没货的 {len(waiting)} 部 ----")
            for series in sorted(waiting, key=lambda s: -s.missing_count)[:top]:
                codes = "、".join(g.code for g in series.gaps[:5])
                more = f" 等 {len(series.gaps)} 集" if len(series.gaps) > 5 else ""
                lines.append(f"  {series.display_name}  缺 {series.missing_count} 集：{codes}{more}")
            lines.append("")

        if self.errors:
            lines.append("---- 出错的源 ----")
            for err in self.errors[:10]:
                lines.append(f"  {err}")
            lines.append("")

        return "\n".join(lines).rstrip() + "\n"

    def render_json(self, *, indent: int = 2, item_limit: int = 3) -> str:
        return json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "elapsed_seconds": round(self.elapsed, 2),
                "feeds_ok": self.feeds_ok,
                "feeds_failed": self.feeds_failed,
                "feed_items": self.feed_items,
                "scanned_series": self.scanned_series,
                "series_with_gaps": self.series_with_gaps,
                "total_missing": self.total_missing,
                "total_covered": self.total_covered,
                "errors": self.errors,
                "series": [s.to_dict(item_limit=item_limit) for s in self.checks],
            },
            ensure_ascii=False,
            indent=indent,
        )


class GapFinder:
    """抓 RSS + 拿库里的缺口，做交叉比对。"""

    def __init__(self, settings: Settings, http: Any) -> None:
        self.settings = settings
        self.http = http

    # ------------------------------------------------------------------
    async def collect_feed_items(
        self,
        subs: Iterable[Subscription] | None = None,
        *,
        include_show_feeds: bool = True,
    ) -> tuple[list[FeedItem], int, int, list[str]]:
        """抓取所有订阅的 RSS，返回 (条目, 成功数, 失败数, 错误列表)。

        feed 与 show 模式的源都会抓 —— 查漏时"哪个源里有"比"订阅了哪部剧"更重要。
        """
        subs = list(subs if subs is not None else self.settings.subscriptions)
        targets: list[tuple[str, str]] = []
        for sub in subs:
            if not sub.enabled:
                continue
            if not include_show_feeds and not sub.is_feed:
                continue
            for url in sub.rss_urls:
                targets.append((sub.name, url))

        # 同一个地址只抓一次
        seen: set[str] = set()
        items: list[FeedItem] = []
        ok = failed = 0
        errors: list[str] = []
        for name, url in targets:
            if url in seen:
                continue
            seen.add(url)
            try:
                fetched = await fetch_feed(self.http, url, retries=2, timeout=30.0)
                items.extend(fetched)
                ok += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                errors.append(f"[{name}] {exc}")
                log.warning("[%s] 查漏时抓取 RSS 失败：%s", name, exc)
        return items, ok, failed, errors

    # ------------------------------------------------------------------
    def cross_check(
        self,
        scan: ScanResult,
        items: list[FeedItem],
        *,
        max_series: int = 0,
        max_gaps_per_series: int = 0,
    ) -> GapReport:
        """把扫描出来的缺口和 RSS 条目对上（纯计算，不发请求，方便测试）。"""
        report = GapReport(feed_items=len(items))
        results: list[SeriesGap] = []

        # 先把 RSS 条目按 (季, 集) 建索引，避免每条缺口都遍历一遍全部条目
        by_episode: dict[int, list[FeedItem]] = {}
        for item in items:
            if item.episode is None:
                continue
            by_episode.setdefault(item.episode, []).append(item)

        candidates = [s for s in scan.series if s.status == STATUS_PARTIAL and s.missing_codes]
        report.scanned_series = len(candidates)
        if max_series:
            candidates = candidates[:max_series]

        for series in candidates:
            gaps: list[GapMatch] = []
            codes = series.missing_codes[:max_gaps_per_series] if max_gaps_per_series else series.missing_codes
            for code in codes:
                season, episode = parse_code(code)
                if episode is None:
                    continue
                matches = [
                    it for it in by_episode.get(episode, []) if item_matches_code(it, season, episode)
                ]
                gaps.append(
                    GapMatch(
                        season=season if season is not None else 0,
                        episode=episode,
                        code=code,
                        name="",
                        items=matches,
                    )
                )
            if not gaps:
                continue
            results.append(
                SeriesGap(
                    name=series.name,
                    display_name=series.display_name,
                    tmdb_id=series.tmdb_id,
                    emby_id=series.emby_id,
                    owned=series.owned,
                    total=series.total,
                    gaps=gaps,
                )
            )

        report.checks = results
        report.series_with_gaps = len(results)
        report.finished_at = time.time()
        return report

    async def run(
        self,
        scan: ScanResult,
        *,
        max_series: int = 0,
        max_gaps_per_series: int = 0,
        subs: Iterable[Subscription] | None = None,
    ) -> GapReport:
        """抓 RSS 并交叉比对。"""
        items, ok, failed, errors = await self.collect_feed_items(subs)
        report = self.cross_check(
            scan, items, max_series=max_series, max_gaps_per_series=max_gaps_per_series
        )
        report.feeds_ok = ok
        report.feeds_failed = failed
        report.errors = errors
        return report
