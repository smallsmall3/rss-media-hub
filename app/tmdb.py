"""TMDB 客户端：拿一部剧的"全部集数"和播出时间线。

TV 相关接口只有两个用得上，全部走 v3 + api_key query 参数，方便挂反代：
  GET /tv/{id}                → 剧集基本信息、季列表、last_episode_to_air
  GET /tv/{id}/season/{n}     → 该季每一集的 air_date / episode_number
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)


class TmdbError(RuntimeError):
    pass


class TmdbNotConfigured(TmdbError):
    pass


class TmdbNotFound(TmdbError):
    pass


@dataclass
class EpisodeInfo:
    season: int
    episode: int
    name: str = ""
    air_date: date | None = None
    runtime: int | None = None

    @property
    def code(self) -> str:
        return f"S{self.season:02d}E{self.episode:02d}"


@dataclass
class SeasonInfo:
    number: int
    name: str = ""
    episode_count: int = 0
    air_date: date | None = None
    episodes: list[EpisodeInfo] = field(default_factory=list)


@dataclass
class SeriesInfo:
    tmdb_id: int
    name: str = ""
    original_name: str = ""
    year: int | None = None
    poster_path: str = ""
    overview: str = ""
    status: str = ""
    total_episodes: int = 0
    total_seasons: int = 0
    seasons: list[SeasonInfo] = field(default_factory=list)

    def season(self, number: int) -> SeasonInfo | None:
        for s in self.seasons:
            if s.number == number:
                return s
        return None

    @property
    def regular_seasons(self) -> list[int]:
        return [s.number for s in self.seasons]


def _parse_date(raw: Any) -> date | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _year_of(raw: Any) -> int | None:
    if not raw or not isinstance(raw, str):
        return None
    match = re.match(r"^(\d{4})", raw.strip())
    return int(match.group(1)) if match else None


class TmdbClient:
    def __init__(
        self,
        api_key: str,
        *,
        api_base: str = "https://api.themoviedb.org/3",
        language: str = "zh-CN",
        image_base: str = "https://image.tmdb.org/t/p/w500",
        timeout: float = 20.0,
        retries: int = 3,
        proxy: str = "",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.api_base = api_base.rstrip("/")
        self.language = language
        self.image_base = image_base.rstrip("/")
        self.timeout = timeout
        self.retries = max(1, retries)
        self.proxy = proxy or ""
        self._client = client
        self._owned = client is None
        self._cache: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "TmdbClient":
        await self._ensure_client()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            kwargs: dict[str, Any] = {}
            if self.proxy:
                kwargs["proxy"] = self.proxy
            try:
                self._client = httpx.AsyncClient(
                    timeout=self.timeout,
                    headers={"Accept": "application/json", "User-Agent": "rss-media-hub/1.0"},
                    **kwargs,
                )
            except ImportError as exc:  # socks 代理需要额外依赖
                raise TmdbError(
                    f"代理 {self.proxy} 需要 httpx 的 socks 支持：pip install 'httpx[socks]'（{exc}）"
                ) from exc
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owned:
            await self._client.aclose()
            self._client = None

    @contextlib.contextmanager
    def fast(self, *, retries: int = 1, timeout: float = 8.0):
        """临时切换到"快速失败"模式，给预检这类要当场出结果的场景用。"""
        old = (self.retries, self.timeout)
        self.retries, self.timeout = max(1, retries), timeout
        try:
            yield self
        finally:
            self.retries, self.timeout = old

    # ------------------------------------------------------------------
    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        retries: int | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """请求 TMDB。retries/timeout 可临时覆盖（预检会传更"急"的值）。"""
        if not self.api_key:
            raise TmdbNotConfigured("未配置 RMH_TMDB_API_KEY，无法查询 TMDB")
        client = await self._ensure_client()
        query = {"api_key": self.api_key, "language": self.language}
        query.update(params or {})
        attempts = max(1, retries if retries is not None else self.retries)
        req_timeout = timeout if timeout is not None else self.timeout
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                resp = await client.get(f"{self.api_base}{path}", params=query, timeout=req_timeout)
                if resp.status_code == 404:
                    raise TmdbNotFound(f"TMDB 找不到资源：{path}")
                if resp.status_code == 401:
                    raise TmdbError("TMDB 鉴权失败（401）：请检查 RMH_TMDB_API_KEY 是否为 v3 api_key")
                if resp.status_code == 429:
                    raise TmdbError("TMDB 限流（429）")
                resp.raise_for_status()
                return resp.json()
            except TmdbNotFound:
                raise
            except TmdbError:
                raise
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt >= attempts:
                    break
                await asyncio.sleep(min(10.0, 1.5 * attempt))
        raise TmdbError(f"请求 TMDB 失败 {path}：{last}") from last

    # ------------------------------------------------------------------
    async def search_tv(self, name: str, year: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"query": name, "include_adult": "false"}
        if year:
            params["first_air_date_year"] = year
        data = await self._get("/search/tv", params)
        results = data.get("results") or []
        if not results and year:
            params.pop("first_air_date_year", None)
            data = await self._get("/search/tv", params)
            results = data.get("results") or []
        return results

    def pick_best(self, results: list[dict[str, Any]], name: str, year: int | None = None) -> dict[str, Any] | None:
        """从搜索结果里挑最像的那部：优先精确名 + 年份。"""
        if not results:
            return None
        target = _normalize(name)

        def score(item: dict[str, Any]) -> tuple[int, int, float]:
            names = {_normalize(item.get("name") or ""), _normalize(item.get("original_name") or "")}
            exact = 1 if target and target in names else 0
            partial = 1 if target and any(target in n or n in target for n in names if n) else 0
            year_hit = 1 if year and _year_of(item.get("first_air_date")) == year else 0
            return (exact, year_hit * 2 + partial, float(item.get("popularity") or 0))

        best = max(results, key=score)
        return best

    async def resolve(
        self,
        name: str | None,
        tmdb_id: int | None = None,
        year: int | None = None,
    ) -> dict[str, Any]:
        """拿到 TMDB 的 tv 对象（含 season 列表）。优先用 id，其次搜索。"""
        if tmdb_id:
            return await self._get(f"/tv/{int(tmdb_id)}")
        if not name:
            raise TmdbError("既没有 tmdb_id 也没有剧名，无法匹配 TMDB")
        results = await self.search_tv(name, year)
        best = self.pick_best(results, name, year)
        if not best:
            raise TmdbNotFound(f"TMDB 搜不到剧名「{name}」")
        matched_name = best.get("name") or best.get("original_name") or ""
        if _normalize(name) not in {_normalize(matched_name), _normalize(best.get("original_name") or "")}:
            log.info("TMDB 名称模糊匹配：%s → %s (%s)", name, matched_name, _year_of(best.get("first_air_date")))
        return await self._get(f"/tv/{best['id']}")

    async def series(
        self,
        name: str | None,
        tmdb_id: int | None = None,
        year: int | None = None,
        *,
        include_specials: bool = False,
        max_seasons: int = 60,
    ) -> SeriesInfo:
        """返回剧集结构 + 每一集的播出日期（用于判断"已播出")。"""
        cache_key = f"series:{tmdb_id}:{name}:{year}:{include_specials}"
        async with self._lock:
            cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        raw = await self.resolve(name, tmdb_id, year)
        info = SeriesInfo(
            tmdb_id=int(raw.get("id") or 0),
            name=raw.get("name") or "",
            original_name=raw.get("original_name") or "",
            year=_year_of(raw.get("first_air_date")),
            poster_path=raw.get("poster_path") or "",
            overview=raw.get("overview") or "",
            status=raw.get("status") or "",
            total_episodes=int(raw.get("number_of_episodes") or 0),
            total_seasons=int(raw.get("number_of_seasons") or 0),
        )

        wanted = [
            int(s.get("season_number") or 0)
            for s in (raw.get("seasons") or [])
            if isinstance(s, dict)
            and (include_specials or int(s.get("season_number") or 0) > 0)
            and int(s.get("season_number") or 0) <= max_seasons
        ]
        wanted = sorted(set(wanted))
        if not wanted:
            log.warning("TMDB 剧集 %s 没有任何常规季，可能数据异常", info.name)

        async def load(number: int) -> SeasonInfo:
            try:
                data = await self._get(
                    f"/tv/{info.tmdb_id}/season/{number}",
                    {"append_to_response": ""},
                )
            except TmdbNotFound:
                return SeasonInfo(number=number, episode_count=0)
            except TmdbError as exc:
                log.warning("TMDB 拉取 %s S%02d 失败：%s", info.name, number, exc)
                return SeasonInfo(number=number, episode_count=0)
            eps = []
            for e in data.get("episodes") or []:
                if not isinstance(e, dict):
                    continue
                num = e.get("episode_number")
                if not isinstance(num, int):
                    continue
                eps.append(
                    EpisodeInfo(
                        season=int(e.get("season_number") or number),
                        episode=num,
                        name=e.get("name") or "",
                        air_date=_parse_date(e.get("air_date")),
                        runtime=e.get("runtime"),
                    )
                )
            eps.sort(key=lambda x: x.episode)
            return SeasonInfo(
                number=number,
                name=data.get("name") or "",
                episode_count=len(eps),
                air_date=_parse_date(data.get("air_date")),
                episodes=eps,
            )

        seasons = await asyncio.gather(*(load(n) for n in wanted))
        info.seasons = list(seasons)
        if not info.total_seasons:
            info.total_seasons = len(wanted)
        if not info.total_episodes:
            info.total_episodes = sum(s.episode_count for s in info.seasons)

        async with self._lock:
            self._cache[cache_key] = info
        return info

    def poster_url(self, path: str | None, size: str = "w500") -> str:
        if not path:
            return ""
        base = self.image_base
        if "/t/p/" in base:
            base = base.split("/t/p/")[0] + f"/t/p/{size}"
        return f"{base}{path}"


def _normalize(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[\s\-_.:：·'\"!！?？,，。()（）\[\]【】]+", "", text)
    for word in ("the", "a", "season", "第", "季"):
        if word in {"the", "a"}:
            text = re.sub(rf"\b{word}\b", "", text)
    return text.strip()


def episodes_upto_today(info: SeriesInfo, today: date | None = None) -> int:
    """统计 TMDB 视角下"已经播出"的集数（没有播出日期的按已播出处理）。"""
    today = today or datetime.now(timezone.utc).date()
    count = 0
    for season in info.seasons:
        for ep in season.episodes:
            if ep.air_date is None or ep.air_date <= today:
                count += 1
    return count


def next_airing(info: SeriesInfo, today: date | None = None) -> EpisodeInfo | None:
    today = today or datetime.now(timezone.utc).date()
    upcoming = [
        ep
        for season in info.seasons
        for ep in season.episodes
        if ep.air_date is not None and ep.air_date > today
    ]
    upcoming.sort(key=lambda e: (e.air_date or today, e.season, e.episode))
    return upcoming[0] if upcoming else None
