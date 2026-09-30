"""核心比对逻辑：TMDB 的"全部集数" × Emby/Jellyfin 的"已入库集数"。

一次 reconcile 做四件事：
  1. 从 TMDB 拿剧集结构（全部集数 / 每集播出日期）
  2. 从 Emby/Jellyfin 拿本地已有的 (季, 集) 集合
  3. 算出 入库数 / 已播出数 / 全部集数，以及缺哪些集
  4. 顺便算出"新入库但还没推送过"的集（用于停机恢复 + 追完判定）

判定"追完"的口径：
  * 统计范围（默认只算已播出）内的每一集都能在媒体库里找到 → 完成
  * 特别篇(Season 0)默认不计入，避免永远追不完
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from .config import LibrarySettings, Subscription
from .db import SubState
from .emby import EmbyClient, EmbyError, LocalSeries
from .tmdb import (
    EpisodeInfo,
    SeriesInfo,
    TmdbClient,
    TmdbError,
    TmdbNotFound,
    episodes_upto_today,
    next_airing,
)

log = logging.getLogger(__name__)


@dataclass
class MissingEpisode:
    season: int
    episode: int
    name: str = ""
    air_date: date | None = None

    @property
    def code(self) -> str:
        return f"S{self.season:02d}E{self.episode:02d}"


@dataclass
class ReconcileResult:
    ok: bool = False
    subscription_id: str = ""
    series: SeriesInfo | None = None
    local: LocalSeries | None = None
    tmdb_id: int | None = None
    # 集数统计
    total: int = 0          # 统计范围内的全部集数（默认只算已播出）
    aired: int = 0          # 其中已播出
    owned: int = 0          # 媒体库里已有的集数
    season_owned: dict[int, int] = field(default_factory=dict)
    season_total: dict[int, int] = field(default_factory=dict)
    missing: list[MissingEpisode] = field(default_factory=list)
    # 新入库
    new_keys: list[str] = field(default_factory=list)
    new_codes: list[str] = field(default_factory=list)
    reduced: bool = False   # 本地集数比上次少（用户删了文件？）
    error: str = ""
    checked_at: int = field(default_factory=lambda: int(time.time()))

    @property
    def done(self) -> bool:
        return self.ok and self.total > 0 and self.owned >= self.total

    @property
    def percent(self) -> float:
        if not self.total:
            return 0.0
        return min(100.0, self.owned / self.total * 100.0)

    def missing_ranges(self, limit: int = 12) -> str:
        """把缺失集压缩成 S01E03-E07 这种区间文本。"""
        if not self.missing:
            return ""
        codes = [(m.season, m.episode) for m in self.missing]
        chunks: list[str] = []
        start = prev = codes[0]
        for cur in codes[1:]:
            if cur[0] == prev[0] and cur[1] == prev[1] + 1:
                prev = cur
                continue
            chunks.append(_fmt_range(start, prev))
            start = prev = cur
        chunks.append(_fmt_range(start, prev))
        if len(chunks) > limit:
            return "、".join(chunks[:limit]) + f" 等 {len(self.missing)} 集"
        return "、".join(chunks)

    def as_state(
        self,
        sub: Subscription,
        previous: SubState | None = None,
        note: str | None = None,
    ) -> SubState:
        state = "done" if self.done else "active"
        return SubState(
            id=sub.id,
            name=sub.name,
            state=state,
            total=self.total,
            aired=self.aired,
            owned=self.owned,
            missing=self.missing_ranges(limit=6),
            tmdb_id=self.tmdb_id,
            last_check=self.checked_at,
            last_notify=previous.last_notify if previous else 0,
            done_at=(previous.done_at if previous and previous.done_at else self.checked_at) if self.done else None,
            last_error="",
            # extra 用来放"已登记 N 条历史条目"这类备注，巡检时不能把它抹掉
            extra=note if note is not None else (previous.extra if previous else ""),
        )


def _fmt_range(start: tuple[int, int], end: tuple[int, int]) -> str:
    if start == end:
        return f"S{start[0]:02d}E{start[1]:02d}"
    if start[0] == end[0]:
        return f"S{start[0]:02d}E{start[1]:02d}-E{end[1]:02d}"
    return f"S{start[0]:02d}E{start[1]:02d}-S{end[0]:02d}E{end[1]:02d}"


class Reconciler:
    """把 TMDB + 媒体库 + 订阅规则捏成一份可推送的对比结果。"""

    def __init__(
        self,
        tmdb: TmdbClient,
        library: EmbyClient,
        settings: LibrarySettings,
    ) -> None:
        self.tmdb = tmdb
        self.library = library
        self.settings = settings
        self._local_cache: dict[str, tuple[float, LocalSeries | None]] = {}

    # ------------------------------------------------------------------
    async def local_series(
        self,
        name: str,
        *,
        year: int | None = None,
        tmdb_id: int | None = None,
        tvdb_id: str | None = None,
    ) -> LocalSeries | None:
        cache_key = f"{name}|{year}|{tmdb_id}|{tvdb_id}"
        ttl = max(0, self.settings.cache_ttl)
        now = time.time()
        cached = self._local_cache.get(cache_key)
        if cached and now - cached[0] < ttl:
            return cached[1]
        try:
            series = await self.library.find_series(name, year=year, tmdb_id=tmdb_id, tvdb_id=tvdb_id)
        except EmbyError as exc:
            log.warning("查询媒体库失败（%s）：%s", name, exc)
            raise
        self._local_cache[cache_key] = (now, series)
        return series

    def invalidate(self, sub_id: str | None = None) -> None:
        self._local_cache.clear()

    # ------------------------------------------------------------------
    async def reconcile(self, sub: Subscription, previous: SubState | None = None) -> ReconcileResult:
        result = ReconcileResult(subscription_id=sub.id)
        try:
            await self.library.detect_kind()
        except Exception:  # noqa: BLE001
            pass

        # ---------- 1) TMDB ----------
        try:
            season_filter = sub.season
            include_specials = self.settings.include_specials or season_filter == 0
            series = await self.tmdb.series(
                sub.name,
                tmdb_id=sub.tmdb_id,
                year=sub.year,
                include_specials=include_specials,
            )
        except (TmdbError, TmdbNotFound) as exc:  # type: ignore[misc]
            result.error = f"TMDB 查询失败：{exc}"
            return result
        except Exception as exc:  # noqa: BLE001
            result.error = f"TMDB 查询异常：{exc}"
            return result

        result.series = series
        result.tmdb_id = series.tmdb_id

        # ---------- 2) 统计范围 ----------
        scope: list[EpisodeInfo] = []
        for season in series.seasons:
            if season_filter is not None and season.number != season_filter:
                continue
            if season.number == 0 and not (self.settings.include_specials or season_filter == 0):
                continue
            scope.extend(season.episodes)

        if not scope:
            result.error = (
                f"TMDB 上《{series.name}》在指定范围内没有剧集"
                f"（season={season_filter}）；请检查订阅的 season 配置"
            )
            return result

        today = datetime.now(timezone.utc).date()
        aired_eps = [ep for ep in scope if ep.air_date is None or ep.air_date <= today]
        result.aired = len(aired_eps)
        counted = aired_eps if self.settings.count_aired_only else scope
        result.total = len(counted)

        # ---------- 3) 本地媒体库 ----------
        try:
            local = await self.local_series(
                sub.name,
                year=sub.year,
                tmdb_id=series.tmdb_id,
            )
        except EmbyError as exc:
            result.error = f"媒体库查询失败：{exc}"
            return result

        if local is None:
            result.error = ""  # 不算错误：可能就是还没入库
            log.info("媒体库里还没有《%s》，入库数按 0 计", sub.name)
            owned_codes: set[tuple[int, int]] = set()
        else:
            result.local = local
            owned_codes = local.episode_codes()

        result.owned = sum(1 for ep in counted if (ep.season, ep.episode) in owned_codes)
        for ep in counted:
            result.season_total[ep.season] = result.season_total.get(ep.season, 0) + 1
            if (ep.season, ep.episode) in owned_codes:
                result.season_owned[ep.season] = result.season_owned.get(ep.season, 0) + 1
            else:
                result.missing.append(
                    MissingEpisode(season=ep.season, episode=ep.episode, name=ep.name, air_date=ep.air_date)
                )

        # ---------- 4) 新入库 ----------
        if previous and previous.owned and result.owned < previous.owned:
            result.reduced = True
        if previous and previous.owned >= 0 and result.owned > previous.owned and local is not None:
            result.new_codes = self._new_codes(previous, result)
        elif previous is None and result.owned and local is not None:
            # 第一次巡检：不把已有库存当作"新入库"，避免上线就刷屏
            pass

        result.ok = True
        return result

    def _new_codes(self, previous: SubState, result: ReconcileResult) -> list[str]:
        """本地库里比上次多出来的集（按 TMDB 顺序输出）。"""
        if result.local is None or result.series is None:
            return []
        delta = result.owned - max(0, previous.owned)
        if delta <= 0:
            return []
        owned_pairs = sorted(
            {
                (ep.season, ep.episode)
                for season in result.series.seasons
                for ep in season.episodes
                if (ep.season, ep.episode) in result.local.episode_codes()
            }
        )
        window = owned_pairs[-delta:]
        return [f"S{s:02d}E{e:02d}" for s, e in window]

    def next_airing(self, result: ReconcileResult) -> EpisodeInfo | None:
        if not result.series:
            return None
        return next_airing(result.series)


def build_progress_bar(owned: int, total: int, width: int = 14) -> str:
    if total <= 0:
        return "░" * width
    filled = int(round(width * min(1.0, owned / total)))
    return "█" * filled + "░" * (width - filled)
