"""给单条 RSS 条目配海报。

show 模式（按剧追踪）本来就有海报 —— 那是从 Emby 里取的，一部剧固定一张。
feed 模式（订阅源全量）不一样：一条推送里可能有好几部不同的片子，
每条都要自己的海报，而条目上**没有 TMDB ID**，只有一条标题。

所以流程是：

    发布标题  →  titleparse 还原片名+年份  →  TMDB 搜索  →  海报字节

三道「宁缺勿错」的闸门（因为没有人工确认，一旦搜错就会推一张完全不相干的图）：
  1. 标题解析没把握 → 不给海报
  2. 没搜到结果 → 不给海报
  3. 搜到的结果和片名不够像 → 不给海报

搜索与图片结果都带缓存：一个源里同一部剧重复出现很常见（多版本、多季），
不缓存的话会把 TMDB 打爆。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .titleparse import parse_release_title, search_terms

log = logging.getLogger(__name__)

# 命中 TMDB 结果要求的最低相似度（0~1）
MIN_SIMILARITY = 0.55

# 海报缓存上限（条），避免长跑之后内存一直涨
MAX_ENTRIES = 300


def normalize_for_compare(text: str) -> str:
    """归一化片名用于比较：去掉标点、空格、大小写。"""
    out = []
    for ch in (text or "").lower():
        if ch.isalnum():
            out.append(ch)
        # 中日韩字符 isalnum() 为真，会保留
    return "".join(out)


def similarity(a: str, b: str) -> float:
    """片名相似度。

    用"较短串被较长串包含" + 字符集合的 Jaccard 混合判断，
    不引入 difflib 之类的重依赖，也够用：
      * "超新星" vs "超新星"            → 1.0
      * "超新星" vs "超新星 第一季"      → 高（包含）
      * "超新星" vs "超新星纪元"        → 中等
      * "超新星" vs "完全无关的剧"       → 低
    """
    x, y = normalize_for_compare(a), normalize_for_compare(b)
    if not x or not y:
        return 0.0
    if x == y:
        return 1.0
    short, long_ = (x, y) if len(x) <= len(y) else (y, x)
    if short in long_:
        # 包含关系：短串越长越可信（避免"星"匹配到一切）
        return 0.75 + 0.25 * (len(short) / max(1, len(long_)))
    # 连续子串重叠度：能区分"超新星"和"超新星纪元"，比字符集合更贴近直觉
    common = 0
    for size in range(min(len(x), len(y)), 1, -1):
        for i in range(len(x) - size + 1):
            if x[i : i + size] in y:
                common = size
                break
        if common:
            break
    overlap = common / max(1, min(len(x), len(y)))
    # 字符集合的 Jaccard 只作辅助。
    # 不能取 max(jaccard, overlap)：像 Oppenheimer / Friends 这种，
    # 光靠共享字母 e/n 就能拿到 0.36，会虚高。要求两者都像才给高分。
    jaccard = len(set(x) & set(y)) / max(1, len(set(x) | set(y)))
    return 0.4 * jaccard + 0.6 * overlap


@dataclass
class PosterHit:
    """一次成功的海报解析结果。"""

    data: bytes                      # 海报字节
    name: str = ""                   # TMDB 匹配名（zh-CN 中文名）
    tmdb_id: int = 0                 # TMDB 剧集 id（一键订阅用）
    year: int | None = None          # 首播年份

    def __bool__(self) -> bool:
        return bool(self.data)


class PosterResolver:
    """按标题找海报。找不到就返回 None（调用方退化为纯文字推送）。"""

    def __init__(
        self,
        tmdb: Any,
        *,
        enabled: bool = True,
        image_base: str = "https://image.tmdb.org/t/p/w500",
        size: str = "w500",
        proxy: str = "",
        timeout: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.tmdb = tmdb
        self.enabled = enabled
        self.image_base = (image_base or "").rstrip("/")
        # 统一成 w500：PT 推送不需要原图
        if "/t/p/" in self.image_base:
            self.image_base = self.image_base.split("/t/p/")[0] + f"/t/p/{size}"
        self.size = size
        self.proxy = proxy or ""
        self.timeout = timeout
        self._client = client
        self._owned = client is None
        self._cache: dict[str, bytes | None] = {}
        self._detail_cache: dict[str, PosterHit | None] = {}
        self._searches = 0
        self._cache_hits = 0

    async def aclose(self) -> None:
        if self._client is not None and self._owned:
            await self._client.aclose()
            self._client = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            kwargs: dict[str, Any] = {"timeout": self.timeout, "follow_redirects": True}
            if self.proxy:
                kwargs["proxy"] = self.proxy
            self._client = httpx.AsyncClient(**kwargs)
        return self._client

    @property
    def stats(self) -> dict[str, int]:
        return {"searches": self._searches, "cache_hits": self._cache_hits, "cached": len(self._detail_cache)}

    def _remember(self, key: str, value: bytes | None) -> None:
        if len(self._cache) >= MAX_ENTRIES:
            # 简单的 FIFO：先清掉最早的一批，别做 LRU 那么复杂
            for k in list(self._cache)[: MAX_ENTRIES // 3]:
                self._cache.pop(k, None)
        self._cache[key] = value

    def _remember_detail(self, key: str, value: PosterHit | None) -> None:
        if len(self._detail_cache) >= MAX_ENTRIES:
            for k in list(self._detail_cache)[: MAX_ENTRIES // 3]:
                self._detail_cache.pop(k, None)
        self._detail_cache[key] = value

    async def resolve(self, title: str) -> bytes | None:
        """按发布标题找海报字节；找不到返回 None。

        会依次尝试多个搜索词：先主片名（拉丁/拼音），再中文别名。
        这是因为国产动漫常用拼音当标题，而 TMDB 上只有中文条目 ——
        `Su Dong Po Yu Hang Zhou De Gu Shi` 搜 0 条，
        但标题尾部方括号里的中文名能搜到。
        """
        hit = await self.resolve_detail(title)
        return hit.data if hit else None

    async def resolve_detail(self, title: str) -> PosterHit | None:
        """按发布标题找海报 + TMDB 中文名 + tmdb_id；找不到返回 None。

        相比 resolve() 多返回匹配名和 tmdb_id：推送标题用中文名
        （The Girl in Blue → 佳期如梦），一键订阅按钮用 tmdb_id。
        """
        if not self.enabled:
            return None
        parsed = parse_release_title(title)
        if not parsed.title or not parsed.confident:
            return None

        key = f"{parsed.title}|{parsed.year or ''}"
        if key in self._detail_cache:
            self._cache_hits += 1
            return self._detail_cache[key]

        result = None
        for term in search_terms(title):
            result = await self._fetch_detail(term, parsed.year)
            if result:
                break
        self._remember_detail(key, result)
        return result

    async def download_poster(self, poster_path: str) -> bytes | None:
        """按 TMDB 的 poster_path（如 /abc.jpg）下载海报字节。

        与 resolve() 不同：这里不做标题解析和相似度判断，
        适合「已经拿到明确 TMDB 条目」的场景（比如「已添加订阅」通知）。
        """
        if not poster_path:
            return None
        return await self._download(poster_path)

    async def _fetch_detail(self, name: str, year: int | None) -> PosterHit | None:
        """搜索 TMDB 并下载海报，返回 PosterHit；找不到返回 None。"""
        if not self._image_base_ok():
            return None
        try:
            self._searches += 1
            results = await self.tmdb.search_tv(name, year)
        except Exception as exc:  # noqa: BLE001
            log.debug("搜索 TMDB 失败（%s）：%s", name, exc)
            return None

        best = self._pick(results, name, year)
        if not best:
            log.debug("TMDB 没搜到够像的结果：%s (%s)", name, year)
            return None

        path = best.get("poster_path")
        if not path:
            return None
        data = await self._download(path)
        if data is None:
            return None
        matched = best.get("name") or best.get("original_name") or name
        return PosterHit(
            data=data,
            name=str(matched),
            tmdb_id=int(best.get("id") or 0),
            year=_year_of(best.get("first_air_date")),
        )

    def _pick(self, results: list[dict[str, Any]], name: str, year: int | None) -> dict[str, Any] | None:
        """从搜索结果里挑最可信的一个。不够像就返回 None。"""
        scored: list[tuple[float, dict[str, Any]]] = []
        for item in results or []:
            titles = [item.get("name") or "", item.get("original_name") or ""]
            score = max(similarity(name, t) for t in titles if t)
            item_year = _year_of(item.get("first_air_date"))
            if year and item_year:
                if item_year == year:
                    score += 0.25
                elif abs(item_year - year) <= 1:
                    score += 0.1
                else:
                    score -= 0.2
            scored.append((score, item))
        if not scored:
            return None
        scored.sort(key=lambda pair: pair[0], reverse=True)
        best_score, best = scored[0]
        if best_score < MIN_SIMILARITY:
            log.debug("最像的结果也只有 %.2f 分（%s），放弃", best_score, name)
            return None
        return best

    def _image_base_ok(self) -> bool:
        return bool(self.image_base and self.image_base.startswith("http"))

    async def _download(self, poster_path: str) -> bytes | None:
        url = f"{self.image_base}{poster_path}"
        try:
            client = await self._http()
            resp = await client.get(url)
            if resp.status_code != 200 or not resp.content:
                log.debug("下载海报失败：HTTP %s", resp.status_code)
                return None
            return resp.content
        except Exception as exc:  # noqa: BLE001
            log.debug("下载海报异常：%s", exc)
            return None


def _year_of(date_text: Any) -> int | None:
    """从 '2020-05-01' 里取年份。"""
    if not date_text:
        return None
    text = str(date_text)
    if len(text) >= 4 and text[:4].isdigit():
        return int(text[:4])
    return None
