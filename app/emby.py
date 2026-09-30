"""Emby / Jellyfin 客户端：查询"这部剧在媒体库里已经有哪些集"。

两者 API 同源，差异点都在这一个文件里抹平：
  * 鉴权：Emby 用 `X-Emby-Token` 或 `api_key`；Jellyfin 用 `Authorization: MediaBrowser
    Token="..."`。为兼容，两个头一起发，服务端只认自己认识的那个。
  * 剧集查询：/Shows/{id}/Episodes 需要 userId（老版 Emby 必填）。没配 userId 时
    自动去 /Users 拿第一个管理员。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import httpx

log = logging.getLogger(__name__)


class EmbyError(RuntimeError):
    pass


class EmbyNotConfigured(EmbyError):
    pass


@dataclass
class LocalEpisode:
    season: int
    episode: int
    name: str = ""
    item_id: str = ""
    air_date: date | None = None
    path: str = ""

    @property
    def code(self) -> str:
        return f"S{self.season:02d}E{self.episode:02d}"


@dataclass
class LocalSeries:
    item_id: str
    name: str
    year: int | None = None
    provider_ids: dict[str, str] = field(default_factory=dict)
    image_id: str = ""
    episodes: list[LocalEpisode] = field(default_factory=list)
    index_number: int | None = None

    def episode_codes(self, season: int | None = None) -> set[tuple[int, int]]:
        out = set()
        for ep in self.episodes:
            if season is not None and ep.season != season:
                continue
            out.add((ep.season, ep.episode))
        return out

    def tmdb_id(self) -> int | None:
        for key in ("Tmdb", "tmdb", "TheMovieDb"):
            value = self.provider_ids.get(key)
            if value and str(value).isdigit():
                return int(value)
        return None

    def tvdb_id(self) -> str:
        for key in ("Tvdb", "tvdb", "TheTVDB"):
            value = self.provider_ids.get(key)
            if value:
                return str(value)
        return ""


def _parse_dt(raw: Any) -> date | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.strptime(raw[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def _norm(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[\s\-_.:：·'\"!！?？,，。()（）\[\]【】]+", "", text)
    return text.strip()


class EmbyClient:
    def __init__(
        self,
        url: str,
        api_key: str,
        *,
        user_id: str = "",
        kind: str = "auto",
        verify_tls: bool = False,
        timeout: float = 20.0,
        proxy: str = "",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.url = (url or "").rstrip("/")
        self.api_key = api_key or ""
        self.user_id = user_id or ""
        self.kind = (kind or "auto").lower()
        self.verify_tls = verify_tls
        self.timeout = timeout
        self.proxy = proxy or ""
        self._client = client
        self._owned = client is None
        self._resolved_kind: str | None = None if self.kind == "auto" else self.kind

    # ------------------------------------------------------------------
    async def __aenter__(self) -> "EmbyClient":
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
                    verify=self.verify_tls,
                    headers=self._headers(),
                    **kwargs,
                )
            except ImportError as exc:  # socks 代理需要额外依赖
                raise EmbyError(
                    f"代理 {self.proxy} 需要 httpx 的 socks 支持：pip install 'httpx[socks]'（{exc}）"
                ) from exc
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owned:
            await self._client.aclose()
            self._client = None

    @contextlib.contextmanager
    def fast(self, *, timeout: float = 5.0):
        """临时切换到更短的超时，给预检这类要当场出结果的场景用。"""
        old = self.timeout
        self.timeout = timeout
        try:
            yield self
        finally:
            self.timeout = old

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "rss-media-hub/1.0",
            "X-Emby-Token": self.api_key,
            "X-MediaBrowser-Token": self.api_key,
            "Authorization": f'MediaBrowser Token="{self.api_key}", Client="rss-media-hub", '
            f'Device="docker", DeviceId="rss-media-hub", Version="1.0.0"',
        }
        return headers

    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        retries: int = 3,
        timeout: float | None = None,
    ) -> Any:
        """请求媒体服务器。retries/timeout 可临时覆盖（预检会传更"急"的值）。"""
        if not self.url or not self.api_key:
            raise EmbyNotConfigured("未配置 RMH_EMBY_URL / RMH_EMBY_API_KEY")
        client = await self._ensure_client()
        query = dict(params or {})
        if self.api_key:
            query.setdefault("api_key", self.api_key)
        req_timeout = timeout if timeout is not None else self.timeout
        last: Exception | None = None
        for attempt in range(1, max(1, retries) + 1):
            try:
                resp = await client.get(f"{self.url}{path}", params=query, timeout=req_timeout)
                if resp.status_code == 401:
                    raise EmbyError("Emby/Jellyfin 鉴权失败（401）：请检查 API 密钥")
                resp.raise_for_status()
                if not resp.content:
                    return {}
                return resp.json()
            except EmbyError:
                raise
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt >= max(1, retries):
                    break
                await asyncio.sleep(min(10.0, 1.5 * attempt))
        raise EmbyError(f"请求 {self.url}{path} 失败：{last}") from last

    async def _get_bytes(self, path: str, params: dict[str, Any] | None = None) -> bytes | None:
        if not self.url:
            return None
        client = await self._ensure_client()
        query = dict(params or {})
        query.setdefault("api_key", self.api_key)
        try:
            resp = await client.get(f"{self.url}{path}", params=query)
            resp.raise_for_status()
            return resp.content
        except Exception as exc:  # noqa: BLE001
            log.debug("拉取图片失败 %s：%s", path, exc)
            return None

    # ------------------------------------------------------------------
    async def detect_kind(self) -> str:
        """区分 Emby 还是 Jellyfin（只影响日志和少量路径，接口是兼容的）。"""
        if self._resolved_kind:
            return self._resolved_kind
        try:
            info = await self._get("/System/Info/Public", retries=1)
            product = str(info.get("ProductName") or info.get("ServerName") or "")
            if "jellyfin" in product.lower():
                self._resolved_kind = "jellyfin"
            elif "emby" in product.lower():
                self._resolved_kind = "emby"
            else:
                self._resolved_kind = "emby"
        except Exception:  # noqa: BLE001
            self._resolved_kind = "emby"
        return self._resolved_kind

    async def resolve_user_id(self) -> str:
        if self.user_id:
            return self.user_id
        users = await self._get("/Users")
        if isinstance(users, list) and users:
            admin = next((u for u in users if (u.get("Policy") or {}).get("IsAdministrator")), users[0])
            self.user_id = str(admin.get("Id") or "")
            log.info("自动选用 %s 用户：%s", await self.detect_kind(), admin.get("Name"))
        return self.user_id

    async def ping(self) -> dict[str, Any]:
        try:
            info = await self._get("/System/Info/Public", retries=1)
        except Exception as exc:  # noqa: BLE001
            raise EmbyError(f"连接 Emby/Jellyfin 失败：{exc}") from exc
        return info

    # ------------------------------------------------------------------
    async def search_series(self, name: str, limit: int = 10) -> list[dict[str, Any]]:
        data = await self._get(
            "/Items",
            {
                "Recursive": "true",
                "IncludeItemTypes": "Series",
                "SearchTerm": name,
                "Limit": limit,
                "Fields": "ProviderIds,ProductionYear,Path,ImageTags",
            },
        )
        return list(data.get("Items") or [])

    async def list_series(
        self,
        *,
        limit_total: int = 0,
        page_size: int = 200,
        on_progress: Any = None,
    ) -> list[dict[str, Any]]:
        """枚举媒体库里的**全部**剧集（分页拉取）。

        这是"以媒体库为订阅源"的基础能力：先知道库里有什么，才能反过来查漏。
        limit_total=0 表示不限数量。
        """
        user_id = await self.resolve_user_id()
        fields = "ProviderIds,ProductionYear,Path,ImageTags,ChildCount,RecursiveItemCount,Status,DateCreated"
        out: list[dict[str, Any]] = []
        start = 0
        while True:
            params: dict[str, Any] = {
                "Recursive": "true",
                "IncludeItemTypes": "Series",
                "StartIndex": start,
                "Limit": page_size,
                "SortBy": "SortName",
                "SortOrder": "Ascending",
                "Fields": fields,
            }
            if user_id:
                params["UserId"] = user_id
            data = await self._get("/Items", params)
            batch = list(data.get("Items") or [])
            if not batch:
                break
            out.extend(batch)
            if on_progress:
                try:
                    on_progress(len(out), int(data.get("TotalRecordCount") or 0))
                except Exception:  # noqa: BLE001
                    pass
            start += len(batch)
            total = int(data.get("TotalRecordCount") or 0)
            if len(batch) < page_size:
                break
            if total and start >= total:
                break
            if limit_total and len(out) >= limit_total:
                break
        if limit_total:
            out = out[:limit_total]
        return out

    async def find_series(
        self,
        name: str,
        *,
        year: int | None = None,
        tmdb_id: int | None = None,
        tvdb_id: str | None = None,
    ) -> LocalSeries | None:
        """在媒体库里定位一部剧：优先外部 ID，其次名称+年份打分。"""
        candidates = await self.search_series(name, limit=25)
        if not candidates and tmdb_id is None:
            candidates = await self.search_series(name.split()[0] if " " in name else name, limit=25)

        def provider_of(item: dict[str, Any]) -> dict[str, str]:
            return {str(k): str(v) for k, v in (item.get("ProviderIds") or {}).items()}

        # 1) 外部 ID 命中，直接返回
        for item in candidates:
            pid = provider_of(item)
            for key in ("Tmdb", "tmdb", "TheMovieDb"):
                if tmdb_id and pid.get(key) and str(pid[key]) == str(tmdb_id):
                    return await self._build_series(item, pid)
            for key in ("Tvdb", "tvdb", "TheTVDB"):
                if tvdb_id and pid.get(key) and str(pid[key]) == str(tvdb_id):
                    return await self._build_series(item, pid)

        # 2) 名称 + 年份打分
        target = _norm(name)
        best: tuple[tuple[int, int], dict[str, Any]] | None = None
        for item in candidates:
            item_name = _norm(item.get("Name") or "")
            if not item_name:
                continue
            exact = 1 if item_name == target else 0
            partial = 1 if (target in item_name or item_name in target) else 0
            if not (exact or partial):
                continue
            year_hit = 1 if year and int(item.get("ProductionYear") or 0) == year else 0
            key = (exact * 2 + partial + year_hit, year_hit)
            if best is None or key > best[0]:
                best = (key, item)
        if best is None:
            return None
        return await self._build_series(best[1], provider_of(best[1]))

    async def _build_series(self, item: dict[str, Any], provider_ids: dict[str, str]) -> LocalSeries:
        series = LocalSeries(
            item_id=str(item.get("Id") or ""),
            name=item.get("Name") or "",
            year=int(item.get("ProductionYear") or 0) or None,
            provider_ids=provider_ids,
            image_id=str((item.get("ImageTags") or {}).get("Primary") or item.get("Id") or ""),
            index_number=item.get("IndexNumber"),
        )
        series.episodes = await self.episodes_of(series.item_id)
        return series

    async def episodes_of(self, series_id: str) -> list[LocalEpisode]:
        user_id = await self.resolve_user_id()
        params: dict[str, Any] = {
            "Fields": "Path,ProviderIds,PremiereDate,ProductionYear",
            "IsMissing": "false",
        }
        if user_id:
            params["UserId"] = user_id
        data = await self._get(f"/Shows/{series_id}/Episodes", params)
        out: list[LocalEpisode] = []
        for item in data.get("Items") or []:
            season = item.get("ParentIndexNumber")
            number = item.get("IndexNumber")
            if season is None or number is None:
                continue
            try:
                season_i, number_i = int(season), int(number)
            except (TypeError, ValueError):
                continue
            location = (item.get("LocationType") or "").lower()
            if location and location != "filesystem":
                continue  # 跳过虚拟条目（未真正入库的）
            out.append(
                LocalEpisode(
                    season=season_i,
                    episode=number_i,
                    name=item.get("Name") or "",
                    item_id=str(item.get("Id") or ""),
                    air_date=_parse_dt(item.get("PremiereDate")),
                    path=item.get("Path") or "",
                )
            )
        out.sort(key=lambda e: (e.season, e.episode))
        return out

    async def poster_bytes(self, series: LocalSeries) -> bytes | None:
        if not series.item_id:
            return None
        image_id = series.image_id or series.item_id
        return await self._get_bytes(
            f"/Items/{series.item_id}/Images/Primary",
            {"tag": image_id, "maxWidth": 600, "quality": 90},
        )
