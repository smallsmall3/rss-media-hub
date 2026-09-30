"""RSS/Atom 抓取与解析。

针对 PT 站的真实情况做了这些处理：
1. 优先用 feedparser（容忍不规范 XML）；没装则退回标准库 ElementTree。
2. 下载链接解析优先级：enclosure.url > torrent:magneturi > 正文里的 magnet > link。
   PT 站的 enclosure 里带 passkey，直接可用，不需要额外登录态。
3. 体积解析：支持 "1.2 GB"、"12,345,678" 字节、torrent:contentLength 等写法。
4. 季集解析：覆盖 S01E02 / 第05集 / EP05 / 全12集 / - 05 等常见命名。
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Iterable

try:  # httpx 只在实际抓取时才需要；解析函数保持零依赖，方便离线单测
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

EPISODE_MAX = 2000  # 防止把分辨率/年份误判成集数

# S01E02 / s1e2 / 1x02
RE_SEASON_EPISODE = re.compile(r"(?<![a-z0-9])s(\d{1,2})[\s._-]?e(\d{1,4})(?![0-9])", re.IGNORECASE)
RE_X_FORMAT = re.compile(r"(?<![a-z0-9])(\d{1,2})x(\d{2,4})(?![0-9])", re.IGNORECASE)
# 第 5 集 / 第五集不做（中文数字太容易误判），只做阿拉伯数字
RE_CN_EPISODE = re.compile(r"第\s*(\d{1,4})\s*[集话話]")
# EP05 / E05 / 第05话
RE_EP = re.compile(r"(?<![a-z0-9])(?:ep|e)\s*(\d{1,4})(?![0-9])", re.IGNORECASE)
# [05] / - 05 / _05 这类纯数字集号（动漫/日剧常见），放在最后兜底
RE_BARE = re.compile(r"(?<![0-9])(\d{1,4})(?=\s*(?:\[|\(|】|v2|end|fin|$|\.mkv|\.mp4))", re.IGNORECASE)
# 全12集 / 共12集 / 12话全
RE_BATCH = re.compile(r"(?:全|共)\s*(\d{1,3})\s*[集话話]|(\d{1,3})\s*[集话話]\s*全")

RE_SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*(TB|GB|GiB|MB|MiB|KB|KiB|B)(?![a-z])", re.IGNORECASE)
_SIZE_UNITS = {
    "b": 1,
    "kb": 10**3,
    "kib": 1024,
    "mb": 10**6,
    "mib": 1024**2,
    "gb": 10**9,
    "gib": 1024**3,
    "tb": 10**12,
    "tib": 1024**4,
}

RE_MAGNET = re.compile(r"magnet:\?xt=urn:btih:[^\s\"'<>]+", re.IGNORECASE)

NS = {
    "torrent": "https://github.com/Jackett/Jackett/raw/master/src/Jackett.Common/Models/IndexerConfig/Bespoke/torznab",
    "torznab": "http://torznab.com/schemas/2015/feed",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "atom": "http://www.w3.org/2005/Atom",
    "media": "http://search.yahoo.com/mrss/",
}


@dataclass
class FeedItem:
    title: str
    link: str = ""
    download_url: str = ""
    guid: str = ""
    published: datetime | None = None
    published_raw: str = ""
    size_bytes: int | None = None
    description: str = ""
    season: int | None = None
    episode: int | None = None
    batch_count: int | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """去重键：优先 guid，其次下载链接，最后标题+时间。"""
        for candidate in (self.guid, self.download_url, self.link):
            if candidate:
                return candidate.strip()
        stamp = self.published.isoformat() if self.published else self.published_raw
        return f"{self.title}|{stamp}"

    @property
    def episode_label(self) -> str:
        if self.batch_count:
            return f"全{self.batch_count}集"
        if self.season is not None and self.episode is not None:
            return f"S{self.season:02d}E{self.episode:02d}"
        if self.episode is not None:
            return f"E{self.episode:02d}"
        return ""

    @property
    def size_text(self) -> str:
        return human_size(self.size_bytes)


def human_size(size: int | None) -> str:
    if not size:
        return "-"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TB"


def parse_size(text: str | None) -> int | None:
    if not text:
        return None
    text = str(text)
    if text.isdigit():
        return int(text)
    match = RE_SIZE.search(text)
    if not match:
        digits = re.sub(r"[^\d]", "", text)
        return int(digits) if digits.isdigit() and digits else None
    value = float(match.group(1))
    unit = match.group(2).lower()
    return int(value * _SIZE_UNITS.get(unit, 1))


def parse_episode(title: str) -> tuple[int | None, int | None, int | None]:
    """从标题解析 (season, episode, batch_count)。"""
    if not title:
        return None, None, None
    text = html.unescape(title)

    season: int | None = None
    episode: int | None = None

    m = RE_SEASON_EPISODE.search(text)
    if m:
        season, episode = int(m.group(1)), int(m.group(2))
    if episode is None:
        m = RE_X_FORMAT.search(text)
        if m:
            season, episode = int(m.group(1)), int(m.group(2))
    if episode is None:
        # 中文"第x季"
        sm = re.search(r"第\s*([0-9一二三四五六七八九十]{1,2})\s*季", text)
        if sm:
            season = _cn_number(sm.group(1))
        m = RE_CN_EPISODE.search(text)
        if m:
            episode = int(m.group(1))
    if episode is None:
        m = RE_EP.search(text)
        if m:
            candidate = int(m.group(1))
            if 0 < candidate <= EPISODE_MAX:
                episode = candidate
    if episode is None:
        m = RE_BARE.search(text)
        if m:
            candidate = int(m.group(1))
            if 0 < candidate <= EPISODE_MAX:
                episode = candidate

    batch = None
    bm = RE_BATCH.search(text)
    if bm:
        batch = int(bm.group(1) or bm.group(2))

    # 合理性校验：S01E99 之类多半是误判，但动漫允许大集号，只在明确有 SxxExx 时信任
    if episode is not None and episode > EPISODE_MAX:
        episode = None
    return season, episode, batch


_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_number(text: str) -> int | None:
    if text.isdigit():
        return int(text)
    if text in _CN_DIGITS:
        return _CN_DIGITS[text]
    if text.startswith("十") and len(text) == 2:
        return 10 + _CN_DIGITS.get(text[1], 0)
    if "十" in text and len(text) == 3:
        return _CN_DIGITS.get(text[0], 0) * 10 + _CN_DIGITS.get(text[2], 0)
    return None


def _text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return (node.text or "").strip()


def _find_text(node: ET.Element, paths: Iterable[str]) -> str:
    for path in paths:
        found = node.find(path, NS)
        if found is not None and (found.text or "").strip():
            return (found.text or "").strip()
    return ""


def _parse_date(raw: str) -> datetime | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def _extract_magnet(raw: dict) -> str:
    """feedparser 对 magnet 的处理因源而异。

    实测（feedparser 6.0.11）：
      * 自定义命名空间会被改成下划线：`torrent:magnetURI` → `torrent_magneturi`
      * magnet 写在正文里时只会出现在 summary/description 中
    """
    for key in ("magneturi", "magnet_url", "magnet", "torrent_magneturi", "torznab_magneturl"):
        value = raw.get(key)
        if isinstance(value, str) and value.lower().startswith("magnet:"):
            return value.strip()
    for key, value in raw.items():
        if "magnet" in key.lower() and isinstance(value, str) and value.lower().startswith("magnet:"):
            return value.strip()
    for link in raw.get("links") or []:
        href = (link.get("href") or "").strip()
        if href.lower().startswith("magnet:"):
            return href
    blob = " ".join(
        str(part)
        for part in (
            raw.get("summary") or "",
            raw.get("description") or "",
            (raw.get("content") or [{}])[0].get("value") if raw.get("content") else "",
        )
    )
    match = RE_MAGNET.search(html.unescape(blob))
    return match.group(0) if match else ""


def _item_from_mapping(raw: dict) -> FeedItem:
    """把 feedparser 的 entry 归一化成 FeedItem。"""
    title = html.unescape((raw.get("title") or "").strip())
    link = (raw.get("link") or "").strip()
    guid = (raw.get("id") or raw.get("guid") or "").strip()
    summary = raw.get("summary") or raw.get("description") or ""
    content_list = raw.get("content") or []
    content_value = content_list[0].get("value") or "" if content_list else ""
    # summary 里常见 HTML 实体（&lt;br/&gt;），先反转义再找链接和体积
    blob = html.unescape(f"{summary} {content_value}")

    # feedparser 6 把 enclosure 放进 links（rel == "enclosure"），旧版才走 enclosures
    candidates: list[tuple[str, str]] = []
    for enc in raw.get("enclosures") or []:
        candidates.append(((enc.get("href") or enc.get("url") or "").strip(), str(enc.get("length") or "")))
    for item in raw.get("links") or []:
        rel = (item.get("rel") or "").lower()
        if rel == "enclosure":
            candidates.append(((item.get("href") or "").strip(), str(item.get("length") or "")))
    if not link:
        for item in raw.get("links") or []:
            if (item.get("rel") or "alternate").lower() in {"alternate", ""} and item.get("href"):
                link = item["href"].strip()
                break

    magnet = _extract_magnet(raw)
    download_url = ""
    size = None
    for href, length in candidates:
        if not href:
            continue
        if href.lower().startswith("magnet:"):
            magnet = magnet or href
            continue
        if not download_url:
            download_url = href
        size = size or parse_size(length)
    # PT 站的带 passkey 直链优先（可直接投递），其次 magnet，最后才是详情页
    download_url = download_url or magnet or link
    if size is None and blob:
        size = parse_size(blob)

    published = None
    published_raw = raw.get("published") or raw.get("updated") or ""
    if raw.get("published_parsed"):
        try:
            published = datetime(*raw["published_parsed"][:6], tzinfo=timezone.utc)
        except Exception:
            published = None
    published = published or _parse_date(published_raw)

    season, episode, batch = parse_episode(title)
    return FeedItem(
        title=title,
        link=link,
        download_url=download_url,
        guid=guid,
        published=published,
        published_raw=published_raw,
        size_bytes=size,
        description=summary or content_value,
        season=season,
        episode=episode,
        batch_count=batch,
    )


def parse_feed_bytes(content: bytes) -> list[FeedItem]:
    """解析 RSS/Atom 字节流，返回条目列表（顺序与源一致）。"""
    if not content or not content.strip():
        return []

    try:  # pragma: no cover - 环境相关
        import feedparser  # type: ignore

        parsed = feedparser.parse(content)
        if getattr(parsed, "entries", None):
            return [_item_from_mapping(dict(e)) for e in parsed.entries]
        if getattr(parsed, "bozo", 0):
            log.debug("feedparser 报 bozo，但可能仍可用：%s", getattr(parsed, "bozo_exception", ""))
    except ImportError:
        pass
    except Exception as exc:
        log.warning("feedparser 解析失败，回退标准库：%s", exc)

    return _parse_with_etree(content)


def _parse_with_etree(content: bytes) -> list[FeedItem]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        # 有些站会在 XML 前塞 BOM 或空白
        cleaned = content.lstrip(b"\xef\xbb\xbf \r\n\t")
        try:
            root = ET.fromstring(cleaned)
        except ET.ParseError as exc:
            raise ValueError(f"XML 解析失败：{exc}") from exc

    tag = root.tag.split("}")[-1].lower()
    if tag == "feed":  # Atom
        entries = root.findall("atom:entry", NS) or root.findall("entry")
    else:
        channel = root.find("channel")
        if channel is None:
            channel = root
        entries = channel.findall("item")

    items: list[FeedItem] = []
    for entry in entries:
        title = _find_text(entry, ["title", "atom:title"])
        title = html.unescape(title)
        link = _find_text(entry, ["link", "atom:link"])
        if not link:
            for ln in entry.findall("atom:link", NS) + entry.findall("link"):
                href = ln.get("href")
                rel = (ln.get("rel") or "alternate").lower()
                if href and rel in {"alternate", ""}:
                    link = href.strip()
                    break
        guid = _find_text(entry, ["guid", "id", "atom:id"])
        description = _find_text(entry, ["description", "summary", "atom:summary", "content:encoded"])
        published_raw = _find_text(entry, ["pubDate", "published", "atom:updated", "dc:date", "updated"])

        download_url = ""
        size = None
        for enc in entry.findall("enclosure"):
            href = (enc.get("url") or "").strip()
            if href:
                download_url = download_url or href
                size = size or parse_size(enc.get("length"))
        if not download_url:
            magnet = _find_text(entry, ["torrent:magnetURI", "magneturi"])
            if magnet:
                download_url = magnet
        if not download_url:
            for attr in ("torrent:magnetURI", "magnetURI", "torrent:infoHash"):
                node = entry.find(attr, NS)
                if node is not None and (node.text or "").strip():
                    value = node.text.strip()
                    if not value.lower().startswith("magnet:"):
                        value = "magnet:?xt=urn:btih:" + value
                    download_url = value
                    break
        if not download_url:
            node = entry.find("torrent:contentLength", NS)
            if node is not None:
                size = size or parse_size((node.text or "").strip())
        if not download_url:
            match = RE_MAGNET.search(description)
            if match:
                download_url = match.group(0)
        if not download_url:
            download_url = link
        if size is None:
            size = parse_size(description)

        published = _parse_date(published_raw)
        season, episode, batch = parse_episode(title)
        items.append(
            FeedItem(
                title=title,
                link=link,
                download_url=download_url,
                guid=guid,
                published=published,
                published_raw=published_raw,
                size_bytes=size,
                description=description,
                season=season,
                episode=episode,
                batch_count=batch,
            )
        )
    return items


async def fetch_feed(
    client: httpx.AsyncClient,
    url: str,
    *,
    retries: int = 3,
    timeout: float = 30.0,
) -> list[FeedItem]:
    """抓取并解析一个 RSS 地址，带指数退避重试。"""
    if httpx is None:  # pragma: no cover
        raise RuntimeError("缺少 httpx 依赖，无法抓取 RSS：pip install -r requirements.txt")
    last_error: Exception | None = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            resp = await client.get(url, timeout=timeout, follow_redirects=True)
            resp.raise_for_status()
            items = parse_feed_bytes(resp.content)
            log.debug("RSS %s → %d 条", url, len(items))
            return items
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt >= retries:
                break
            delay = min(30.0, 2.0 ** attempt)
            log.warning("抓取 RSS 失败（第 %d/%d 次）：%s — %.0fs 后重试", attempt, retries, exc, delay)
            await asyncio.sleep(delay)
    assert last_error is not None
    raise last_error


def sort_items(items: list[FeedItem]) -> list[FeedItem]:
    """按发布时间升序（没有时间的排在前面，保证输出顺序稳定）。"""
    def sort_key(it: FeedItem):
        ts = it.published.timestamp() if it.published else 0.0
        return (ts, it.title)

    return sorted(items, key=sort_key)
