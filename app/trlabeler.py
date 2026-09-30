"""Transmission 站点标签打标：扫描全库种子，按 tracker 域名贴站点标签。

这是把独立的 tr_labeler.py 能力内置进 rss-media-hub：
  * 连 Transmission RPC（自动处理 409 session-id 握手 + Basic 认证）
  * 读 mappings（域名=标签），扫描全部种子的 tracker
  * 命中映射的种子 → 追加站点标签（**只添加，绝不覆盖/删除已有标签**）
  * 所有 tracker 都不在映射里的种子 → 保持原样，不猜测

安全口径与原版一致：
  * 标签集合 = 原有标签 ∪ 目标标签
  * 预演（dry-run）默认不写入，确认后 apply
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)


class TrError(RuntimeError):
    pass


class TrNotConfigured(TrError):
    pass


def tracker_host(announce: str) -> str | None:
    """从 announce URL 提取小写域名。"""
    if not announce:
        return None
    try:
        host = urlsplit(announce).hostname
        if host:
            return host.lower()
    except ValueError:
        pass
    match = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://([^/:]+)", announce)
    return match.group(1).lower() if match else None


def match_label(host: str, pairs: list[tuple[str, str]]) -> str | None:
    """host 与映射域名相等、或为映射域名的子域时命中，返回标签；否则 None。"""
    for domain, label in pairs:
        if host == domain or host.endswith("." + domain):
            return label
    return None


@dataclass
class LabelMappings:
    """域名→标签 的映射表。"""

    pairs: list[tuple[str, str]] = field(default_factory=list)

    def labels_for(self, hosts: set[str]) -> list[str]:
        """按 hosts 里命中的域名，返回去重后的标签列表（保持映射表顺序）。"""
        seen: list[str] = []
        for host in hosts:
            label = match_label(host, self.pairs)
            if label and label not in seen:
                seen.append(label)
        return seen


def load_mappings(path: Any) -> list[tuple[str, str]]:
    """读取 mappings.txt（域名=标签，一行一条，UTF-8）。

    兼容原版 tr_labeler 的格式与容错：
      * `#` 开头、空行跳过
      * 缺 `=` 或域名/标签为空的行走警告跳过
      * 文件不存在时返回空列表（打标时自然什么都不做）
    """
    from pathlib import Path

    p = Path(path)
    if not p.exists():
        return []
    pairs: list[tuple[str, str]] = []
    for lineno, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            log.warning("mappings.txt 第 %d 行缺少 '='，已跳过：%s", lineno, line)
            continue
        domain, label = line.split("=", 1)
        domain, label = domain.strip().lower(), label.strip()
        if not domain or not label:
            log.warning("mappings.txt 第 %d 行域名或标签为空，已跳过", lineno)
            continue
        pairs.append((domain, label))
    return pairs


class TransmissionClient:
    """Transmission RPC 客户端（httpx 版，兼容 409 会话握手）。"""

    def __init__(
        self,
        url: str,
        *,
        user: str = "",
        password: str = "",
        client: httpx.AsyncClient | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.url = (url or "").rstrip("/")
        self.user = user
        self.password = password
        self.session_id: str | None = None
        self._client = client
        self._owned = client is None
        self._timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    async def aclose(self) -> None:
        if self._client is not None and self._owned:
            await self._client.aclose()
            self._client = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.user:
            import base64

            token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        if self.session_id:
            headers["X-Transmission-Session-Id"] = self.session_id
        return headers

    async def call(self, method: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """调用 RPC，自动处理 409（拿到 session-id 后重试一次）。"""
        if not self.url:
            raise TrNotConfigured("未配置 Transmission 地址")
        client = await self._http()
        payload: dict[str, Any] = {"method": method}
        if arguments is not None:
            payload["arguments"] = arguments

        for attempt in (1, 2):
            resp = await client.post(self.url, json=payload, headers=self._headers())
            if resp.status_code == 409:
                sid = resp.headers.get("X-Transmission-Session-Id")
                if not sid:
                    raise TrError("Transmission 返回 409 但未提供 session-id")
                self.session_id = sid
                continue
            if resp.status_code == 401:
                raise TrError("认证失败 (401)：请检查 Transmission 账号密码")
            resp.raise_for_status()
            data = resp.json()
            if data.get("result") != "success":
                raise TrError(f"RPC {method} 失败：{data.get('result')}")
            return data.get("arguments") or {}
        raise TrError("Transmission 会话握手失败")


@dataclass
class LabelPlan:
    """一次扫描产出的打标计划。"""

    plan: dict[int, list[str]] = field(default_factory=dict)  # torrent_id -> 目标标签
    unmapped: set[str] = field(default_factory=set)
    skipped_unmapped: int = 0
    total: int = 0

    @property
    def changes(self) -> int:
        return len(self.plan)


def collect_plan(torrents: list[dict[str, Any]], mappings: LabelMappings, auto_pt: bool = True) -> LabelPlan:
    """计算每个种子需要追加的标签；返回计划（不写任何东西）。

    与原版 tr_labeler 的 collect_plan 逻辑一致：
      * 提取种子的所有 tracker 域名
      * 命中的标签收集起来，追加到现有标签后面
      * 一个都没命中的种子跳过（不猜、不自动加 PT）
    """
    result = LabelPlan(total=len(torrents))
    for torrent in torrents:
        hosts: set[str] = set()
        for tracker in torrent.get("trackers") or []:
            announce = tracker.get("announce") if isinstance(tracker, dict) else None
            host = tracker_host(announce or "")
            if host:
                hosts.add(host)

        mapped: list[str] = []
        for host in hosts:
            label = match_label(host, mappings.pairs)
            if label:
                if label not in mapped:
                    mapped.append(label)
            else:
                result.unmapped.add(host)

        if not mapped:
            if hosts:
                result.skipped_unmapped += 1
            continue

        current = list(torrent.get("labels") or [])
        target = list(current)
        if auto_pt and "PT" not in target:
            target.insert(0, "PT")
        for label in mapped:
            if label not in target:
                target.append(label)

        if set(target) != set(current):
            result.plan[torrent["id"]] = target

    return result


async def scan_and_label(
    client: TransmissionClient,
    mappings: LabelMappings,
    *,
    apply: bool = False,
    auto_pt: bool = True,
) -> LabelPlan:
    """扫描全库 → 计算计划 → （可选）写入并复核。"""
    session = await client.call("session-get")
    version = session.get("version", "?")

    torrents = (
        await client.call("torrent-get", {"fields": ["id", "name", "labels", "trackers"]})
    ).get("torrents", [])
    result = collect_plan(torrents, mappings, auto_pt=auto_pt)

    if not apply or not result.plan:
        return result

    # 按目标标签分组，一次 RPC 写一批
    grouped: dict[tuple[str, ...], list[int]] = {}
    for tid, labels in result.plan.items():
        grouped.setdefault(tuple(sorted(labels)), []).append(tid)
    for labels, ids in grouped.items():
        await client.call("torrent-set", {"ids": ids, "labels": list(labels)})

    # 复核：确认目标标签都已写上
    verified = (
        await client.call("torrent-get", {"fields": ["id", "labels"]})
    ).get("torrents", [])
    current = {t["id"]: set(t.get("labels") or []) for t in verified}
    bad = [tid for tid, labels in result.plan.items() if set(labels) - current.get(tid, set())]
    if bad:
        raise TrError(f"复核失败 {len(bad)} 个种子：{bad[:20]}")
    return result
