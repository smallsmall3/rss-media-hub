"""媒体库全库扫描：反向枚举 Emby/Jellyfin 里的每一部剧，逐部算「入库 x / 全部 y」。

这是「以媒体库为订阅源」的地基：
  Emby 全库 ──► 逐部匹配 TMDB ──► 算缺口 ──► 报告 / 供查漏模式使用

和 reconcile.py 的区别：
  * reconcile 是「一部剧 × 一个订阅」的定点比对（轮询时会高频调用）
  * 这里是一次性把整库摸清（可能几百部剧），必须考虑 TMDB 限流，所以带持久化缓存
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from .config import LibrarySettings
from .db import Database
from .emby import EmbyClient, EmbyError, LocalSeries
from .tmdb import EpisodeInfo, SeasonInfo, SeriesInfo, TmdbClient, TmdbError, TmdbNotFound

log = logging.getLogger(__name__)

# 单部剧的状态
STATUS_COMPLETE = "complete"      # 已播出范围内全部入库
STATUS_PARTIAL = "partial"        # 有缺集
STATUS_EMPTY = "empty"            # 一集都没入库
STATUS_NEW = "new"                # 一集未播（TMDB 上还没播出）
STATUS_UNMATCHED = "unmatched"    # 没有 TMDB ID 且名称匹配不上
STATUS_ERROR = "error"            # 查询出错

STATUS_LABEL = {
    STATUS_COMPLETE: "✅ 完整",
    STATUS_PARTIAL: "⏳ 缺集",
    STATUS_EMPTY: "🕐 未入库",
    STATUS_NEW: "🆕 未播出",
    STATUS_UNMATCHED: "❓ 未匹配",
    STATUS_ERROR: "❌ 出错",
}


@dataclass
class SeriesScan:
    """一部剧的扫描结果。"""

    emby_id: str
    name: str
    year: int | None = None
    tmdb_id: int | None = None
    tvdb_id: str = ""
    # 集数统计
    total: int = 0          # 统计范围内的全部集数（默认只算已播出）
    aired: int = 0          # 其中已播出
    owned: int = 0          # 库里已有的集数
    status: str = STATUS_UNMATCHED
    missing_codes: list[str] = field(default_factory=list)
    tmdb_name: str = ""
    poster_path: str = ""
    error: str = ""

    @property
    def missing_count(self) -> int:
        return len(self.missing_codes)

    @property
    def percent(self) -> float:
        if not self.total:
            return 0.0
        return min(100.0, self.owned / self.total * 100.0)

    @property
    def display_name(self) -> str:
        base = self.tmdb_name or self.name
        return f"{base}（{self.year}）" if self.year else base

    def missing_ranges(self, limit: int = 3) -> str:
        """把缺失集号压缩成 S02E05-E08 这种区间，最多展示 limit 段。"""
        if not self.missing_codes:
            return ""
        parsed: list[tuple[int, int]] = []
        for code in self.missing_codes:
            try:
                season_part, episode_part = code.upper().lstrip("S").split("E")
                parsed.append((int(season_part), int(episode_part)))
            except (ValueError, AttributeError):
                continue
        if not parsed:
            return "、".join(self.missing_codes[:6])
        parsed.sort()
        chunks: list[str] = []
        start = prev = parsed[0]
        for cur in parsed[1:]:
            if cur[0] == prev[0] and cur[1] == prev[1] + 1:
                prev = cur
                continue
            chunks.append(_fmt(start, prev))
            start = prev = cur
        chunks.append(_fmt(start, prev))
        if len(chunks) > limit:
            return "、".join(chunks[:limit]) + f" 等 {len(self.missing_codes)} 集"
        return "、".join(chunks)

    def to_dict(self) -> dict:
        return {
            "emby_id": self.emby_id,
            "name": self.name,
            "tmdb_name": self.tmdb_name,
            "year": self.year,
            "tmdb_id": self.tmdb_id,
            "tvdb_id": self.tvdb_id,
            "status": self.status,
            "status_label": STATUS_LABEL.get(self.status, self.status),
            "owned": self.owned,
            "total": self.total,
            "aired": self.aired,
            "percent": round(self.percent, 1),
            "missing": self.missing_codes,
            "error": self.error,
        }


def _fmt(start: tuple[int, int], end: tuple[int, int]) -> str:
    if start == end:
        return f"S{start[0]:02d}E{start[1]:02d}"
    if start[0] == end[0]:
        return f"S{start[0]:02d}E{start[1]:02d}-E{end[1]:02d}"
    return f"S{start[0]:02d}E{start[1]:02d}-S{end[0]:02d}E{end[1]:02d}"


@dataclass
class ScanResult:
    """一次全库扫描的汇总。"""

    series: list[SeriesScan] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    library_total: int = 0      # Emby 里一共有多少部剧
    scanned: int = 0            # 实际检查了多少部
    tmdb_calls: int = 0         # 实际打了多少次 TMDB（缓存命中不算）
    cache_hits: int = 0
    truncated: bool = False     # 是否因为数量上限而截断

    @property
    def elapsed(self) -> float:
        end = self.finished_at or time.time()
        return max(0.0, end - self.started_at)

    def by_status(self, status: str) -> list[SeriesScan]:
        return [s for s in self.series if s.status == status]

    @property
    def complete(self) -> list[SeriesScan]:
        return self.by_status(STATUS_COMPLETE)

    @property
    def partial(self) -> list[SeriesScan]:
        """有缺集、且确实缺的是已播出的集 —— 这是"查漏"最该关注的一批。"""
        return self.by_status(STATUS_PARTIAL)

    @property
    def empty(self) -> list[SeriesScan]:
        return self.by_status(STATUS_EMPTY)

    @property
    def unmatched(self) -> list[SeriesScan]:
        return self.by_status(STATUS_UNMATCHED)

    @property
    def errors(self) -> list[SeriesScan]:
        return self.by_status(STATUS_ERROR)

    @property
    def total_missing_episodes(self) -> int:
        return sum(s.missing_count for s in self.partial)

    def render_text(self, *, top: int = 15, show_complete: bool = False) -> str:
        lines: list[str] = []
        lines.append("=" * 78)
        lines.append("媒体库扫描报告")
        lines.append("=" * 78)
        lines.append(
            f"库内剧集 {self.library_total} 部，本次检查 {self.scanned} 部"
            + ("（已按上限截断）" if self.truncated else "")
            + f"，耗时 {self.elapsed:.1f}s"
        )
        lines.append(
            f"TMDB 请求 {self.tmdb_calls} 次，缓存命中 {self.cache_hits} 次"
        )
        lines.append("")
        lines.append(
            f"✅ 完整 {len(self.complete):>4} 部    "
            f"⏳ 缺集 {len(self.partial):>4} 部    "
            f"🕐 未入库 {len(self.empty):>4} 部"
        )
        lines.append(
            f"🆕 未播出 {len(self.by_status(STATUS_NEW)):>3} 部    "
            f"❓ 未匹配 {len(self.unmatched):>4} 部    "
            f"❌ 出错 {len(self.errors):>4} 部"
        )
        lines.append("")

        gaps = sorted(self.partial, key=lambda s: (-s.missing_count, s.display_name))
        if gaps:
            lines.append(f"---- 缺集最多的 {min(top, len(gaps))} 部（共缺 {self.total_missing_episodes} 集）----")
            for item in gaps[:top]:
                lines.append(
                    f"  {item.owned:>4}/{item.total:<4} {item.percent:>5.1f}%  "
                    f"{item.display_name}  缺 {item.missing_ranges()}"
                )
            lines.append("")

        if self.empty:
            lines.append(f"---- 库里一集都没有的 {min(top, len(self.empty))} 部 ----")
            for item in sorted(self.empty, key=lambda s: s.display_name)[:top]:
                lines.append(f"  0/{item.total:<4}        {item.display_name}")
            lines.append("")

        if self.unmatched:
            lines.append(f"---- 未能匹配到 TMDB 的 {len(self.unmatched)} 部（建议在订阅里手工指定 tmdb_id）----")
            for item in sorted(self.unmatched, key=lambda s: s.display_name)[:top]:
                lines.append(f"  {item.name}（{item.year}）  {item.error}")
            lines.append("")

        if self.errors:
            lines.append(f"---- 扫描出错的 {len(self.errors)} 部 ----")
            for item in sorted(self.errors, key=lambda s: s.display_name)[:top]:
                lines.append(f"  {item.display_name}  {item.error}")
            lines.append("")

        if show_complete and self.complete:
            lines.append(f"---- 已完整的 {len(self.complete)} 部 ----")
            for item in sorted(self.complete, key=lambda s: s.display_name):
                lines.append(f"  {item.owned:>4}/{item.total:<4} {item.display_name}")
            lines.append("")

        return "\n".join(lines).rstrip() + "\n"

    def render_json(self, *, indent: int = 2) -> str:
        return json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "library_total": self.library_total,
                "scanned": self.scanned,
                "truncated": self.truncated,
                "elapsed_seconds": round(self.elapsed, 2),
                "tmdb_calls": self.tmdb_calls,
                "cache_hits": self.cache_hits,
                "summary": {
                    "complete": len(self.complete),
                    "partial": len(self.partial),
                    "empty": len(self.empty),
                    "new": len(self.by_status(STATUS_NEW)),
                    "unmatched": len(self.unmatched),
                    "error": len(self.errors),
                    "missing_episodes": self.total_missing_episodes,
                },
                "series": [s.to_dict() for s in self.series],
            },
            ensure_ascii=False,
            indent=indent,
        )


class LibraryScanner:
    """把 Emby 全库和 TMDB 对起来算缺口。"""

    def __init__(
        self,
        tmdb: TmdbClient,
        library: EmbyClient,
        settings: LibrarySettings,
        *,
        db: Database | None = None,
        cache_ttl: int = 12 * 3600,
        concurrency: int = 5,
    ) -> None:
        self.tmdb = tmdb
        self.library = library
        self.settings = settings
        self.db = db
        self.cache_ttl = max(0, cache_ttl)
        self.concurrency = max(1, concurrency)
        self._mem: dict[str, tuple[float, SeriesInfo]] = {}
        # 统计本次扫描真的打了多少次 TMDB / 命中多少次缓存
        self._tmdb_calls = 0
        self._cache_hits = 0

    # ------------------------------------------------------------------
    # TMDB 取数（带持久化缓存 + 进程内缓存）
    # ------------------------------------------------------------------
    async def _series_info(self, name: str, tmdb_id: int | None, year: int | None) -> tuple[SeriesInfo, bool]:
        """返回 (剧集结构, 是否命中缓存)。"""
        cache_key = f"tv:{tmdb_id}" if tmdb_id else f"search:{name}:{year}"
        now = time.time()

        cached = self._mem.get(cache_key)
        if cached and (self.cache_ttl == 0 or now - cached[0] <= self.cache_ttl):
            return cached[1], True

        if self.db is not None:
            raw = await self.db.get_tmdb_cache(cache_key, self.cache_ttl)
            if raw:
                try:
                    info = _series_from_json(json.loads(raw))
                    self._mem[cache_key] = (now, info)
                    return info, True
                except Exception as exc:  # noqa: BLE001
                    log.debug("TMDB 缓存损坏，忽略：%s", exc)

        info = await self.tmdb.series(name, tmdb_id=tmdb_id, year=year, include_specials=self.settings.include_specials)
        self._mem[cache_key] = (now, info)
        if self.db is not None:
            try:
                await self.db.set_tmdb_cache(cache_key, json.dumps(_series_to_json(info), ensure_ascii=False))
            except Exception as exc:  # noqa: BLE001
                log.debug("写 TMDB 缓存失败：%s", exc)
        return info, False

    # ------------------------------------------------------------------
    # 单部剧
    # ------------------------------------------------------------------
    async def scan_one(self, item: dict, *, local: LocalSeries | None = None) -> SeriesScan:
        provider_ids = {str(k): str(v) for k, v in (item.get("ProviderIds") or {}).items()}
        name = item.get("Name") or ""
        year = int(item.get("ProductionYear") or 0) or None
        tmdb_id = None
        for key in ("Tmdb", "tmdb", "TheMovieDb"):
            if provider_ids.get(key, "").isdigit():
                tmdb_id = int(provider_ids[key])
                break
        tvdb_id = next((provider_ids[k] for k in ("Tvdb", "tvdb", "TheTVDB") if provider_ids.get(k)), "")

        result = SeriesScan(emby_id=str(item.get("Id") or ""), name=name, year=year, tmdb_id=tmdb_id, tvdb_id=tvdb_id)

        # ---- TMDB ----
        try:
            info, cached = await self._series_info(name, tmdb_id, year)
            if cached:
                self._cache_hits += 1
            else:
                self._tmdb_calls += 1
        except TmdbNotFound as exc:
            # 库里没有 TMDB ID、按名称也搜不到 —— 这是常见情况，不算故障
            result.status = STATUS_UNMATCHED
            result.error = str(exc) or "TMDB 搜不到这部剧"
            return result
        except TmdbError as exc:
            result.status = STATUS_ERROR
            result.error = str(exc)
            return result
        except Exception as exc:  # noqa: BLE001
            result.status = STATUS_ERROR
            result.error = f"TMDB 查询异常：{exc}"
            return result

        result.tmdb_id = info.tmdb_id or tmdb_id
        result.tmdb_name = info.name
        result.poster_path = info.poster_path
        if not result.year:
            result.year = info.year

        # ---- 统计范围（默认只算已播出集）----
        today = datetime.now(timezone.utc).date()
        scope: list[EpisodeInfo] = []
        for season in info.seasons:
            if season.number == 0 and not self.settings.include_specials:
                continue
            scope.extend(season.episodes)
        aired_eps = [ep for ep in scope if ep.air_date is None or ep.air_date <= today]
        counted = aired_eps if self.settings.count_aired_only else scope
        result.aired = len(aired_eps)
        result.total = len(counted)

        # ---- 库里的集 ----
        try:
            if local is None:
                local = await self._local_series(result.emby_id)
        except EmbyError as exc:
            result.status = STATUS_ERROR
            result.error = f"媒体库查询失败：{exc}"
            return result

        owned_codes = local.episode_codes() if local else set()
        result.owned = sum(1 for ep in counted if (ep.season, ep.episode) in owned_codes)
        result.missing_codes = [
            f"S{ep.season:02d}E{ep.episode:02d}" for ep in counted if (ep.season, ep.episode) not in owned_codes
        ]

        if result.total == 0:
            result.status = STATUS_NEW
        elif not result.owned:
            result.status = STATUS_EMPTY
        elif result.missing_codes:
            result.status = STATUS_PARTIAL
        else:
            result.status = STATUS_COMPLETE
        return result

    async def _local_series(self, series_id: str) -> LocalSeries | None:
        """直接按 Id 拿某部剧的集列表（比按名字搜更准，扫描场景下我们本来就有 Id）。"""
        series = LocalSeries(item_id=series_id, name="")
        series.episodes = await self.library.episodes_of(series_id)
        return series

    # ------------------------------------------------------------------
    # 全库
    # ------------------------------------------------------------------
    async def scan(
        self,
        *,
        limit: int = 0,
        on_progress: Any = None,
        series_filter: Any = None,
    ) -> ScanResult:
        """扫描整个媒体库。limit=0 表示不限。"""
        result = ScanResult()
        self._tmdb_calls = 0
        self._cache_hits = 0
        items = await self.library.list_series(limit_total=limit)
        result.library_total = len(items)
        # 有些实现会忽略 limit_total，这里再兜一层，保证上限一定生效
        targets = [it for it in items if series_filter is None or series_filter(it)]
        if limit:
            targets = targets[:limit]
        result.scanned = len(targets)
        result.truncated = bool(limit) and len(items) > len(targets)

        sem = asyncio.Semaphore(self.concurrency)
        done = 0
        lock = asyncio.Lock()

        async def worker(item: dict) -> SeriesScan:
            nonlocal done
            async with sem:
                try:
                    scan = await self.scan_one(item)
                except Exception as exc:  # noqa: BLE001
                    scan = SeriesScan(
                        emby_id=str(item.get("Id") or ""),
                        name=item.get("Name") or "",
                        status=STATUS_ERROR,
                        error=str(exc),
                    )
            async with lock:
                done += 1
                if on_progress:
                    try:
                        on_progress(done, len(targets), scan)
                    except Exception:  # noqa: BLE001
                        pass
            return scan

        tasks = [asyncio.create_task(worker(it)) for it in targets]
        if tasks:
            collected = await asyncio.gather(*tasks, return_exceptions=True)
            for entry in collected:
                if isinstance(entry, SeriesScan):
                    result.series.append(entry)
                elif isinstance(entry, BaseException):
                    log.warning("扫描任务异常：%s", entry)

        result.finished_at = time.time()
        result.tmdb_calls = self._tmdb_calls
        result.cache_hits = self._cache_hits
        return result


# --------------------------------------------------------------------------
# SeriesInfo <-> JSON（用于持久化缓存）
# --------------------------------------------------------------------------


def _series_to_json(info: SeriesInfo) -> dict:
    return {
        "tmdb_id": info.tmdb_id,
        "name": info.name,
        "original_name": info.original_name,
        "year": info.year,
        "poster_path": info.poster_path,
        "status": info.status,
        "total_episodes": info.total_episodes,
        "total_seasons": info.total_seasons,
        "seasons": [
            {
                "number": s.number,
                "name": s.name,
                "episode_count": s.episode_count,
                "episodes": [
                    {
                        "season": e.season,
                        "episode": e.episode,
                        "name": e.name,
                        "air_date": e.air_date.isoformat() if e.air_date else None,
                    }
                    for e in s.episodes
                ],
            }
            for s in info.seasons
        ],
    }


def _series_from_json(data: dict) -> SeriesInfo:
    seasons = []
    for raw_season in data.get("seasons") or []:
        episodes = []
        for raw_ep in raw_season.get("episodes") or []:
            air = raw_ep.get("air_date")
            episodes.append(
                EpisodeInfo(
                    season=int(raw_ep.get("season") or raw_season.get("number") or 0),
                    episode=int(raw_ep.get("episode") or 0),
                    name=raw_ep.get("name") or "",
                    air_date=date.fromisoformat(air) if air else None,
                )
            )
        seasons.append(
            {
                "number": int(raw_season.get("number") or 0),
                "name": raw_season.get("name") or "",
                "episode_count": int(raw_season.get("episode_count") or len(episodes)),
                "episodes": episodes,
            }
        )
    info = SeriesInfo(
        tmdb_id=int(data.get("tmdb_id") or 0),
        name=data.get("name") or "",
        original_name=data.get("original_name") or "",
        year=data.get("year"),
        poster_path=data.get("poster_path") or "",
        status=data.get("status") or "",
        total_episodes=int(data.get("total_episodes") or 0),
        total_seasons=int(data.get("total_seasons") or 0),
    )
    info.seasons = [
        SeasonInfo(
            number=s["number"],
            name=s["name"],
            episode_count=s["episode_count"],
            episodes=s["episodes"],
        )
        for s in seasons
    ]
    return info
