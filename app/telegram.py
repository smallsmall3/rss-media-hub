"""Telegram 推送。

用 HTML parse_mode：比 MarkdownV2 少踩转义坑（剧名里的 `-` `.` `(` 全都要转义）。
消息长度按 3500 字符安全切分，单条失败不影响后续推送。
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

import httpx

log = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    pass


MAX_LEN = 3500          # 我们主动切分的软上限（留出余量给 HTML 标签）
HARD_LIMIT = 4096       # Telegram 的硬上限，超过直接报错
MIN_TAIL = 400          # 最后一块小于这个长度就尝试并回上一块


def esc(text: Any) -> str:
    """HTML 转义：所有用户可见文本都必须过一遍。"""
    return html.escape(str(text if text is not None else ""), quote=False)


def split_message(text: str, limit: int = MAX_LEN) -> list[str]:
    """按行切分长消息，保证不把 HTML 标签切一半。

    两个细节：
      * 单行超长时按"标签感知"切：优先切在标签之外的 > 处；整段都在标签内部
        就切在 < 之前
      * 最后一块如果短得可怜（几十字符的时间戳尾巴），就并回上一块——
        前提是并完不超过 Telegram 的硬上限，否则宁可多发一条

    关于"标签比 limit 还长"：实测真实推送里最长的标签是 93 字符（PT 下载链接），
    距离 3500 的上限差 37 倍，所以不为这种场景做额外处理——硬切即可，
    而发送端本来就有"parse 失败就去掉 parse_mode 重发"的兜底。
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    buf = ""
    for line in text.splitlines(keepends=True):
        if len(buf) + len(line) > limit and buf:
            chunks.append(buf.rstrip("\n"))
            buf = ""
        while len(line) > limit:  # 单行超长，硬切
            cut = _safe_cut(line, limit)
            chunks.append(line[:cut])
            line = line[cut:]
        buf += line
    if buf.strip():
        chunks.append(buf.rstrip("\n"))

    if len(chunks) >= 2:
        tail = chunks[-1]
        prev = chunks[-2]
        if len(tail) < MIN_TAIL and len(prev) + 2 + len(tail) <= HARD_LIMIT:
            chunks[-2] = f"{prev}\n{tail}"
            chunks.pop()
    return chunks


def _split_raw(text: str, limit: int) -> list[str]:
    chunks: list[str] = []
    buf = ""
    for line in text.splitlines(keepends=True):
        if len(buf) + len(line) > limit and buf:
            chunks.append(buf.rstrip("\n"))
            buf = ""
        while len(line) > limit:  # 单行超长，硬切
            cut = _safe_cut(line, limit)
            chunks.append(line[:cut])
            line = line[cut:]
        buf += line
    if buf.strip():
        chunks.append(buf.rstrip("\n"))

    if len(chunks) >= 2:
        tail = chunks[-1]
        prev = chunks[-2]
        if len(tail) < MIN_TAIL and len(prev) + 2 + len(tail) <= HARD_LIMIT:
            chunks[-2] = f"{prev}\n{tail}"
            chunks.pop()
    return chunks


def _safe_cut(line: str, limit: int) -> int:
    """找一个不会把 HTML 标签切两半的切点。

    返回切分位置；找不到合适位置时退回 limit（保证一定能前进，不会死循环）。
    """
    last_open = line.rfind("<", 0, limit)
    last_close = line.rfind(">", 0, limit)
    inside_tag = last_open > last_close
    if inside_tag:
        # 整段都还在标签内部：切到 < 之前，绝不切开标签
        if last_open > 0:
            return last_open
        return limit
    if last_close > 0:
        return last_close + 1
    return limit


@dataclass
class TgResult:
    ok: bool
    message_id: int | None = None
    error: str = ""


class TelegramSender:
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        *,
        thread_id: str = "",
        api_base: str = "https://api.telegram.org",
        proxy: str = "",
        disable_notification: bool = False,
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.bot_token = bot_token or ""
        self.chat_id = str(chat_id or "")
        self.thread_id = str(thread_id or "")
        self.api_base = (api_base or "https://api.telegram.org").rstrip("/")
        self.proxy = proxy or ""
        self.disable_notification = disable_notification
        self.timeout = timeout
        self._client = client
        self._owned = client is None
        self._token_retry = False  # 自定义反代失败时可回退官方地址

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    async def __aenter__(self) -> "TelegramSender":
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
                self._client = httpx.AsyncClient(timeout=self.timeout, **kwargs)
            except ImportError as exc:  # socks 代理需要额外依赖
                raise TelegramError(
                    f"代理 {self.proxy} 需要 httpx 的 socks 支持：pip install 'httpx[socks]'（{exc}）"
                ) from exc
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owned:
            await self._client.aclose()
            self._client = None

    def _url(self, method: str, base: str | None = None) -> str:
        return f"{base or self.api_base}/bot{self.bot_token}/{method}"

    async def _call(self, method: str, payload: dict[str, Any], files: dict[str, Any] | None = None) -> TgResult:
        if not self.enabled:
            return TgResult(False, error="Telegram 未配置（缺 bot_token 或 chat_id）")
        client = await self._ensure_client()
        data = {k: v for k, v in payload.items() if v not in (None, "")}
        if self.thread_id:
            data.setdefault("message_thread_id", self.thread_id)
        if self.disable_notification:
            data.setdefault("disable_notification", "true")

        bases = [self.api_base]
        if not self._token_retry and "api.telegram.org" not in self.api_base:
            bases.append("https://api.telegram.org")

        last_error = ""
        for base in bases:
            for attempt in range(1, 4):
                try:
                    if files:
                        resp = await client.post(self._url(method, base), data=data, files=files)
                    else:
                        resp = await client.post(self._url(method, base), data=data)
                    if resp.status_code == 429:
                        retry_after = 3
                        try:
                            retry_after = int((resp.json().get("parameters") or {}).get("retry_after") or 3)
                        except Exception:  # noqa: BLE001
                            pass
                        log.warning("Telegram 限流，%ss 后重试", retry_after)
                        await asyncio.sleep(min(60, retry_after + 1))
                        continue
                    body = resp.json()
                    if body.get("ok"):
                        msg = body.get("result") or {}
                        return TgResult(True, message_id=msg.get("message_id"))
                    desc = body.get("description") or resp.text[:200]
                    last_error = f"{resp.status_code} {desc}"
                    # 群组没开话题 / 话题被删：去掉 thread 再试一次
                    if "thread" in desc.lower() and "message_thread_id" in data:
                        data.pop("message_thread_id", None)
                        continue
                    if "parse" in desc.lower() and "parse_mode" in data:
                        data.pop("parse_mode", None)
                        continue
                    break
                except Exception as exc:  # noqa: BLE001
                    last_error = str(exc)
                    await asyncio.sleep(min(10.0, 1.5 * attempt))
            if "api.telegram.org" not in self.api_base:
                self._token_retry = True
        return TgResult(False, error=last_error or "未知错误")

    # ------------------------------------------------------------------
    async def send_message(self, text: str, *, parse_mode: str = "HTML") -> TgResult:
        last = TgResult(False, error="空消息")
        for chunk in split_message(text):
            last = await self._call("sendMessage", {"chat_id": self.chat_id, "text": chunk, "parse_mode": parse_mode,
                                                    "disable_web_page_preview": "true"})
            if not last.ok:
                log.error("Telegram 发送失败：%s", last.error)
                return last
        return last

    async def send_photo(self, photo: bytes, caption: str = "") -> TgResult:
        if not photo:
            return TgResult(False, error="没有图片数据")
        return await self._call(
            "sendPhoto",
            {"chat_id": self.chat_id, "caption": caption[:1024], "parse_mode": "HTML"},
            files={"photo": ("poster.jpg", photo, "image/jpeg")},
        )

    async def send_photo_url(self, url: str, caption: str = "") -> TgResult:
        return await self._call(
            "sendPhoto",
            {"chat_id": self.chat_id, "photo": url, "caption": caption[:1024], "parse_mode": "HTML"},
        )

    async def get_me(self) -> dict[str, Any]:
        client = await self._ensure_client()
        resp = await client.get(self._url("getMe"))
        body = resp.json()
        if not body.get("ok"):
            raise TelegramError(body.get("description") or "getMe 失败")
        return body["result"]
