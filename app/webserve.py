"""网页 UI 的 HTTP 服务：纯标准库 asyncio，不引入 Web 框架。

设计取舍：
  * 用 asyncio.start_server 手写极简路由 —— 镜像里不用多装 Flask/FastAPI 这一坨
  * 服务端只做 JSON API + 单页 HTML；界面逻辑全在前端，方便以后换皮
  * 所有返回值里**密钥一律打码**，只有提交时才接受新值
  * UI 写入的配置进 settings.yaml，不碰用户手写的 config.yaml
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import __version__
from .version import BuildInfo
from .config import UI_EDITABLE, apply_overrides, load_settings, save_subscriptions, Subscription
from .libraryscan import ScanResult
from .trlabeler import LabelMappings

log = logging.getLogger(__name__)

MAX_BODY = 1 << 20  # 1MB，配置提交够用了
MASK_KEEP = 4       # 打码时保留首尾各 4 位


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------


def mask(value: Any, keep: int = MASK_KEEP) -> str:
    """把密钥打码成 abcd****wxyz，方便用户确认"填的是哪个"又不泄露。"""
    text = "" if value is None else str(value)
    if not text:
        return ""
    if len(text) <= keep * 2:
        return "*" * len(text)
    return f"{text[:keep]}{'*' * 8}{text[-keep:]}"


def json_response(payload: Any, status: int = 200) -> tuple[int, str, bytes, dict[str, str]]:
    body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
    return status, "application/json; charset=utf-8", body, {}


def text_response(text: str, status: int = 200, ctype: str = "text/plain; charset=utf-8"):
    return status, ctype, text.encode("utf-8"), {}


def html_response(html: str) -> tuple[int, str, bytes, dict[str, str]]:
    return 200, "text/html; charset=utf-8", html.encode("utf-8"), {}


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Path):
        return str(obj)
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in obj.__dict__.items() if not k.startswith("_")}
    return str(obj)


async def read_request(reader: asyncio.StreamReader) -> tuple[str, str, dict[str, str], bytes]:
    """读一个 HTTP 请求，返回 (method, path, headers, body)。

    只处理我们需要的最小子集：请求行 + 头部 + Content-Length 指定的正文。
    """
    raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
    head, _, rest = raw.partition(b"\r\n\r\n")
    lines = head.decode("utf-8", "replace").split("\r\n")
    if not lines or not lines[0]:
        raise ValueError("空请求")
    parts = lines[0].split()
    method = parts[0].upper() if parts else "GET"
    path = parts[1] if len(parts) > 1 else "/"
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()

    body = b""
    length = 0
    try:
        length = int(headers.get("content-length") or 0)
    except ValueError:
        length = 0
    if length > 0:
        length = min(length, MAX_BODY)
        body = rest
        while len(body) < length:
            chunk = await asyncio.wait_for(reader.read(length - len(body)), timeout=10)
            if not chunk:
                break
            body += chunk
    return method, path, headers, body[:MAX_BODY]


def parse_json_body(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"请求体不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("请求体应该是 JSON 对象")
    return data


def env_overridden() -> dict[str, str]:
    """返回被环境变量控制、UI 改了也不生效的字段（collected from os.environ）。"""
    mapping = {
        "telegram.bot_token": "RMH_TG_BOT_TOKEN",
        "telegram.chat_id": "RMH_TG_CHAT_ID",
        "telegram.thread_id": "RMH_TG_THREAD_ID",
        "telegram.api_base": "RMH_TG_API_BASE",
        "telegram.proxy": "RMH_TG_PROXY",
        "tmdb.api_key": "RMH_TMDB_API_KEY",
        "tmdb.api_base": "RMH_TMDB_API_BASE",
        "tmdb.language": "RMH_TMDB_LANGUAGE",
        "tmdb.proxy": "RMH_TMDB_PROXY",
        "library.url": "RMH_EMBY_URL",
        "library.api_key": "RMH_EMBY_API_KEY",
        "library.user_id": "RMH_EMBY_USER_ID",
        "library.kind": "RMH_EMBY_KIND",
        "library.verify_tls": "RMH_EMBY_VERIFY_TLS",
        "library.count_aired_only": "RMH_EMBY_COUNT_AIRED_ONLY",
        "library.include_specials": "RMH_EMBY_INCLUDE_SPECIALS",
        "runtime.poll_interval": "RMH_POLL_INTERVAL",
        "runtime.reconcile_interval": "RMH_RECONCILE_INTERVAL",
        "runtime.seed_silent": "RMH_SEED_SILENT",
        "runtime.log_level": "RMH_LOG_LEVEL",
        "runtime.scan_cache_ttl": "RMH_SCAN_CACHE_TTL",
        "runtime.scan_concurrency": "RMH_SCAN_CONCURRENCY",
        "runtime.scan_max_series": "RMH_SCAN_MAX_SERIES",
        "ui.token": "RMH_UI_TOKEN",
    }
    out = {}
    for dotted, env_name in mapping.items():
        if (os.environ.get(env_name) or "").strip():
            out[dotted] = env_name
    return out


SECRET_FIELDS = {
    "telegram.bot_token",
    "tmdb.api_key",
    "library.api_key",
    "transmission.password",
    "ui.token",
}


def mask_secret_groups(config: dict[str, Any]) -> dict[str, Any]:
    """把配置里的密钥字段打码。

    GET /api/config 与 POST /api/config 都必须过一遍：
    POST 曾经把 apply_overrides 返回的**明文**配置直接回给浏览器
    （等于把你刚填的 token 又送回来一次，抓包/日志里就泄露了）。
    """
    out: dict[str, Any] = {}
    for group, fields in (config or {}).items():
        if not isinstance(fields, dict):
            out[group] = fields
            continue
        bucket: dict[str, Any] = {}
        for key, value in fields.items():
            if f"{group}.{key}" in SECRET_FIELDS:
                bucket[key] = {"set": bool(value), "masked": mask(value)}
            else:
                bucket[key] = value
        out[group] = bucket
    return out


# --------------------------------------------------------------------------
# WebUI
# --------------------------------------------------------------------------


class WebUI:
    """把 Hub 的能力暴露成 REST API，并托管单页界面。"""

    def __init__(self, hub: Any, *, host: str = "0.0.0.0", port: int = 8080, token: str = "") -> None:
        self.hub = hub
        self.host = host
        # port=0 表示"随便给个空闲端口"（测试用）；负数表示彻底关闭
        self.port = port
        self.token = (token or "").strip()
        self.started_at = time.time()
        self._server: asyncio.AbstractServer | None = None
        self._routes: list[tuple[str, str, Callable[..., Awaitable[Any]]]] = [
            ("GET", "/healthz", self.h_healthz),
            ("GET", "/api/dashboard", self.h_dashboard),
            ("GET", "/api/config", self.h_config_get),
            ("POST", "/api/config", self.h_config_post),
            ("GET", "/api/subscriptions", self.h_subs_get),
            ("POST", "/api/subscriptions", self.h_subs_post),
            ("DELETE", "/api/subscriptions", self.h_subs_delete),
            ("GET", "/api/status", self.h_status),
            ("POST", "/api/scan", self.h_scan),
            ("GET", "/api/scan/last", self.h_scan_last),
            ("POST", "/api/gaps", self.h_gaps),
            ("GET", "/api/gaps/last", self.h_gaps_last),
            ("POST", "/api/feeds/check", self.h_feeds_check),
            ("GET", "/api/feeds/last", self.h_feeds_last),
            ("GET", "/api/selfcheck", self.h_selfcheck),
            ("POST", "/api/preflight", self.h_preflight),
            ("POST", "/api/telegram/test", self.h_tg_test),
            ("POST", "/api/check", self.h_check),
            ("POST", "/api/labels/run", self.h_labels_run),
            ("GET", "/api/labels/mappings", self.h_labels_mappings_get),
            ("POST", "/api/labels/mappings", self.h_labels_mappings_post),
            ("GET", "/", self.h_index),
            ("GET", "/index.html", self.h_index),
        ]
        self.last_scan: ScanResult | None = None
        self.last_gaps: Any = None
        self.last_feeds: Any = None

    # ---------------- 生命周期 ----------------
    async def start(self) -> None:
        if self.port < 0:
            log.info("网页 UI 已关闭（RMH_UI_ENABLED=false）")
            return
        try:
            self._server = await asyncio.start_server(self._on_connection, self.host, self.port)
        except OSError as exc:
            log.warning("网页 UI 端口 %d 监听失败（不影响主功能）：%s", self.port, exc)
            return
        bound = self.bound_port
        if self.token:
            log.info("网页 UI：http://%s:%d/  （已启用口令）", self.host, bound)
        else:
            log.warning(
                "网页 UI：http://%s:%d/  未设置 RMH_UI_TOKEN，同一局域网内任何人都能打开并修改配置",
                self.host,
                bound,
            )

    async def serve_forever(self) -> None:
        await self.start()
        if self._server is None:
            return
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            self._server = None

    @property
    def bound_port(self) -> int:
        if self._server and self._server.sockets:
            return int(self._server.sockets[0].getsockname()[1])
        return self.port

    # ---------------- 请求处理 ----------------
    async def _on_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            method, path, headers, body = await read_request(reader)
        except Exception:  # noqa: BLE001
            with contextlib.suppress(Exception):
                writer.close()
            return

        try:
            status, ctype, payload, extra = await self.dispatch(method, path, headers, body)
        except Exception as exc:  # noqa: BLE001
            log.exception("网页 UI 处理 %s %s 出错", method, path)
            status, ctype, payload, extra = json_response({"ok": False, "error": str(exc)}, 500)

        reason = {200: "OK", 201: "Created", 400: "Bad Request", 401: "Unauthorized",
                  404: "Not Found", 405: "Method Not Allowed", 500: "Internal Server Error"}.get(status, "OK")
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {ctype}\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n"
        )
        for key, value in (extra or {}).items():
            head += f"{key}: {value}\r\n"
        head += "\r\n"
        try:
            writer.write(head.encode("utf-8") + payload)
            await writer.drain()
        except Exception:  # noqa: BLE001
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    def _path_only(self, path: str) -> str:
        return path.split("?", 1)[0].rstrip("/") or "/"

    def _query(self, path: str) -> dict[str, str]:
        if "?" not in path:
            return {}
        from urllib.parse import parse_qs

        return {k: v[0] for k, v in parse_qs(path.split("?", 1)[1]).items()}

    def check_auth(self, headers: dict[str, str], query: dict[str, str]) -> bool:
        if not self.token:
            return True
        supplied = ""
        auth = headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
        supplied = supplied or headers.get("x-rmh-token") or query.get("token") or ""
        return supplied == self.token

    async def dispatch(
        self, method: str, path: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, str, bytes, dict[str, str]]:
        route = self._path_only(path)
        query = self._query(path)

        matched_path = False
        for route_method, route_path, handler in self._routes:
            if route_path != route:
                continue
            matched_path = True
            if route_method != method:
                continue
            # /healthz 不需要口令，方便 Docker healthcheck
            if route != "/healthz" and not self.check_auth(headers, query):
                return json_response({"ok": False, "error": "口令不正确（请在设置里填 RMH_UI_TOKEN）"}, 401)
            try:
                result = await handler(headers, query, body)
            except ValueError as exc:
                return json_response({"ok": False, "error": str(exc)}, 400)
            if isinstance(result, tuple):
                return result
            return json_response(result)
        if matched_path:
            return json_response({"ok": False, "error": f"{route} 不支持 {method}"}, 405)
        return json_response({"ok": False, "error": f"未知路径 {route}"}, 404)

    # ---------------- 各端点 ----------------
    async def h_healthz(self, headers, query, body):
        return text_response("ok")

    async def h_index(self, headers, query, body):
        from .webui import INDEX_HTML

        return html_response(INDEX_HTML)

    def _sub_payload(self, sub: Subscription) -> dict[str, Any]:
        return {
            "id": sub.id,
            "name": sub.name,
            "rss": sub.rss,
            "rss_urls": sub.rss_urls,
            "mode": sub.mode,
            "mode_label": sub.mode_label,
            "seed": sub.seed,
            "tmdb_required": sub.tmdb_required,
            "tmdb_id": sub.tmdb_id,
            "year": sub.year,
            "season": sub.season,
            "enabled": sub.enabled,
            "notify_new": sub.notify_new,
            "remove_when_done": sub.remove_when_done,
            "name_filter": sub.name_filter,
            "exclude_filter": sub.exclude_filter,
            "quality": sub.quality,
            "tags": sub.tags,
            "note": sub.note,
        }

    async def h_dashboard(self, headers, query, body) -> dict[str, Any]:
        hub = self.hub
        stats = hub.stats
        runtimes = list(hub.runtimes.values())
        states = {s.id: s for s in await hub.db.all_subs()}

        subs = []
        for rt in runtimes:
            sub = rt.sub
            state = states.get(sub.id)
            counts = await hub.db.count_items(sub.id)
            subs.append(
                {
                    **self._sub_payload(sub),
                    "state": state.state if state else "unknown",
                    "owned": state.owned if state else 0,
                    "total": state.aired or state.total if state else 0,
                    "aired": state.aired if state else 0,
                    "missing": state.missing if state else "",
                    "last_error": state.last_error if state else "",
                    "done": bool(state and state.is_done),
                    "progress": round((state.progress * 100) if state else 0.0, 1),
                    "items_total": counts[0],
                    "items_notified": counts[1],
                    "last_check": state.last_check if state else 0,
                    "next_poll": rt.next_poll,
                }
            )
        subs.sort(key=lambda s: (s["done"], -s["progress"]))

        return {
            "ok": True,
            "version": __version__,
            "build": BuildInfo.current().to_dict(),
            "now": int(time.time()),
            "uptime_seconds": int(time.time() - (stats.started_at or time.time())),
            "started_at": int(stats.started_at or 0),
            "health": {
                "telegram": hub.settings.telegram.enabled,
                "tmdb": hub.settings.tmdb.enabled,
                "library": hub.settings.library.enabled,
                "library_url": hub.settings.library.url,
                "proxy_tg": bool(hub.settings.telegram.proxy),
                "proxy_tmdb": bool(hub.settings.tmdb.proxy),
            },
            "stats": {
                "polls": stats.polls,
                "feeds_ok": stats.feeds_ok,
                "feeds_failed": stats.feeds_failed,
                "items_seen": stats.items_seen,
                "notifications": stats.notifications,
                "reconcile_runs": stats.reconcile_runs,
                "removals": stats.removals,
                "last_error": stats.last_error,
                "last_poll_at": int(stats.last_poll_at or 0),
                "last_reconcile_at": int(stats.last_reconcile_at or 0),
            },
            "subscriptions": subs,
            "scan_available": hub.settings.library.enabled and hub.settings.tmdb.enabled,
            "last_scan": _scan_summary(self.last_scan),
        }

    async def h_config_get(self, headers, query, body) -> dict[str, Any]:
        settings = self.hub.settings
        overrides = {}
        try:
            from .config import load_yaml_file

            overrides = load_yaml_file(settings.overrides_file, default={}) or {}
        except Exception:  # noqa: BLE001
            overrides = {}

        def group(name: str, source: dict[str, Any]) -> dict[str, Any]:
            out: dict[str, Any] = {}
            for field in UI_EDITABLE[name]:
                dotted = f"{name}.{field}"
                value = source.get(field)
                if dotted in SECRET_FIELDS:
                    out[field] = {"set": bool(value), "masked": mask(value)}
                else:
                    out[field] = value
            return out

        library_raw = {
            "url": settings.library.url,
            "api_key": settings.library.api_key,
            "user_id": settings.library.user_id,
            "kind": settings.library.kind,
            "count_aired_only": settings.library.count_aired_only,
            "include_specials": settings.library.include_specials,
            "verify_tls": settings.library.verify_tls,
            "cache_ttl": settings.library.cache_ttl,
            "proxy": settings.library.proxy,
        }
        runtime_raw = {
            "poll_interval": settings.poll_interval,
            "reconcile_interval": settings.reconcile_interval,
            "seed_silent": settings.seed_silent,
            "log_level": settings.log_level,
            "scan_cache_ttl": settings.scan_cache_ttl,
            "scan_concurrency": settings.scan_concurrency,
            "scan_max_series": settings.scan_max_series,
        }
        tg_raw = asdict(settings.telegram)
        tmdb_raw = asdict(settings.tmdb)
        tr_raw = {
            "url": settings.transmission.url,
            "user": settings.transmission.user,
            "password": settings.transmission.password,
            "enabled": settings.transmission.enabled,
            "interval": settings.transmission.interval,
            "auto_pt": settings.transmission.auto_pt,
        }

        return {
            "ok": True,
            "editable": UI_EDITABLE,
            "secret_fields": sorted(SECRET_FIELDS),
            "env_overridden": env_overridden(),
            "overrides_file": str(settings.overrides_file),
            # 必须打码：这里是 settings.yaml 的原始内容，含明文密钥
            "overrides": mask_secret_groups(overrides),
            "values": {
                "telegram": group("telegram", tg_raw),
                "tmdb": group("tmdb", tmdb_raw),
                "library": group("library", library_raw),
                "transmission": group("transmission", tr_raw),
                "runtime": group("runtime", runtime_raw),
                "ui": {
                    "enabled": settings.ui_enabled,
                    "token": {"set": bool(settings.ui_token), "masked": mask(settings.ui_token)},
                },
            },
        }

    async def h_config_post(self, headers, query, body) -> dict[str, Any]:
        payload = parse_json_body(body)
        changes = payload.get("changes")
        if not isinstance(changes, dict):
            raise ValueError("缺少 changes 字段（形如 {\"changes\": {\"telegram\": {\"bot_token\": \"...\"}}}）")
        if not changes:
            raise ValueError("changes 是空的，没有要保存的内容")

        result = apply_overrides(self.hub.settings.config_dir, changes=changes)

        # 热重载：重新读一遍配置，并重建客户端（换 token/地址必须重建）
        rediscovered = load_settings(self.hub.settings.config_dir, self.hub.settings.state_dir)
        await self.hub.apply_settings(rediscovered)
        self.hub.reload_subscriptions_file()

        # 确认真的生效了，别让"保存成功"变成假象
        applied = {
            "poll_interval": self.hub.settings.poll_interval,
            "reconcile_interval": self.hub.settings.reconcile_interval,
            "scan_concurrency": self.hub.settings.scan_concurrency,
            "scan_max_series": self.hub.settings.scan_max_series,
            "telegram_ready": self.hub.settings.telegram.enabled,
            "tmdb_ready": self.hub.settings.tmdb.enabled,
            "library_ready": self.hub.settings.library.enabled,
            "library_url": self.hub.settings.library.url,
            "transmission_ready": self.hub.settings.transmission.configured,
            "proxy_tg": self.hub.settings.telegram.proxy,
            "proxy_tmdb": self.hub.settings.tmdb.proxy,
        }
        return {
            "ok": True,
            # 必须打码：这里曾经把明文密钥回给浏览器
            "saved": mask_secret_groups(result["settings"]),
            "ignored": result["ignored"],
            "applied": applied,
            "restart_recommended": False,
            "message": "已保存并热重载（无需重启容器）",
        }

    async def h_subs_get(self, headers, query, body) -> dict[str, Any]:
        return {
            "ok": True,
            "subscriptions": [self._sub_payload(s) for s in self.hub.settings.subscriptions],
        }

    async def h_subs_post(self, headers, query, body) -> dict[str, Any]:
        from .config import parse_subscription

        payload = parse_json_body(body)
        raw = payload.get("subscription") or payload
        sub = parse_subscription(raw)  # 会做完整校验，缺字段直接报错
        existing = {s.id for s in self.hub.settings.subscriptions}
        if sub.id in existing and not payload.get("overwrite"):
            raise ValueError(f"订阅 id 已存在：{sub.id}（想覆盖请传 overwrite: true）")

        subs = [s for s in self.hub.settings.subscriptions if s.id != sub.id]
        is_new = sub.id not in existing
        subs.append(sub)
        save_subscriptions(self.hub.settings.subs_file, subs)
        self.hub.reload_subscriptions_file()
        if is_new:
            # 新订阅 → 后台发一条「已添加订阅」确认（查 TMDB/下载海报可能要
            # 几秒，别让网页请求干等）。覆盖已有订阅（overwrite）不算新增。
            self.hub.spawn_bg(self.hub.announce_subscription(sub))
        return {"ok": True, "subscription": self._sub_payload(sub), "message": f"已保存订阅「{sub.name}」"}

    async def h_subs_delete(self, headers, query, body) -> dict[str, Any]:
        target = query.get("id") or ""
        payload: dict[str, Any] = {}
        if not target:
            try:
                payload = parse_json_body(body)
            except ValueError:
                payload = {}
            target = str(payload.get("id") or "")
        if not target:
            raise ValueError("需要提供要删除的订阅 id")

        sub = next((s for s in self.hub.settings.subscriptions if s.id == target or s.name == target), None)
        if sub is None:
            raise ValueError(f"找不到订阅：{target}")

        if payload.get("purge"):
            await self.hub.db.drop_items(sub.id)
            await self.hub.db.delete_sub(sub.id)
        await self.hub.remove_subscription(sub, reason="网页 UI 手动删除")
        return {"ok": True, "message": f"已删除订阅「{sub.name}」"}

    async def h_status(self, headers, query, body) -> dict[str, Any]:
        states = await self.hub.db.all_subs()
        return {
            "ok": True,
            "version": __version__,
            "build": BuildInfo.current().to_dict(),
            "uptime_seconds": int(time.time() - (self.hub.stats.started_at or time.time())),
            "subscriptions": [
                {
                    "id": s.id,
                    "name": s.name,
                    "state": s.state,
                    "owned": s.owned,
                    "total": s.total,
                    "aired": s.aired,
                    "missing": s.missing,
                    "last_error": s.last_error,
                }
                for s in states
            ],
        }

    async def h_scan(self, headers, query, body) -> dict[str, Any]:
        hub = self.hub
        if not hub.settings.library.enabled:
            raise ValueError("没有配置 Emby/Jellyfin，无法扫描")
        if not hub.settings.tmdb.enabled:
            raise ValueError("没有配置 TMDB API Key，无法比对集数")

        payload: dict[str, Any] = {}
        try:
            payload = parse_json_body(body)
        except ValueError:
            payload = {}
        limit = payload.get("limit")
        try:
            limit_int = int(limit) if limit not in (None, "") else hub.settings.scan_max_series
        except (TypeError, ValueError):
            limit_int = hub.settings.scan_max_series

        if payload.get("refresh"):
            await hub.db.purge_tmdb_cache(hub.settings.scan_cache_ttl)

        result = await hub.scanner.scan(limit=limit_int)
        self.last_scan = result
        return {
            "ok": True,
            "summary": _scan_summary(result),
            "result": json.loads(result.render_json()),
        }

    async def h_scan_last(self, headers, query, body) -> dict[str, Any]:
        if self.last_scan is None:
            return {"ok": True, "result": None}
        return {
            "ok": True,
            "summary": _scan_summary(self.last_scan),
            "result": json.loads(self.last_scan.render_json()),
        }

    async def h_gaps(self, headers, query, body) -> dict[str, Any]:
        """查漏：媒体库缺的集 × 当前 RSS 里现成的资源。

        会先复用/执行一次媒体库扫描（很贵，所以默认复用 last_scan），
        再抓一次 RSS 做交叉比对。
        """
        hub = self.hub
        if not hub.settings.library.enabled:
            raise ValueError("没有配置 Emby/Jellyfin，无法查漏")
        if not hub.settings.tmdb.enabled:
            raise ValueError("没有配置 TMDB API Key，无法比对集数")
        if not hub.settings.subscriptions:
            raise ValueError("还没有任何订阅（RSS 源），没有可比的资源")

        payload: dict[str, Any] = {}
        try:
            payload = parse_json_body(body)
        except ValueError:
            payload = {}

        scan = None
        rescan = bool(payload.get("rescan"))
        reused = False
        if not rescan and self.last_scan is not None:
            scan = self.last_scan
            reused = True
        if scan is None:
            limit = payload.get("limit")
            try:
                limit_int = int(limit) if limit not in (None, "") else hub.settings.scan_max_series
            except (TypeError, ValueError):
                limit_int = hub.settings.scan_max_series
            scan = await hub.scanner.scan(limit=limit_int)
            self.last_scan = scan

        report = await hub.gapfinder.run(scan)
        self.last_gaps = report
        return {
            "ok": True,
            "reused_scan": reused,
            "summary": {
                "feed_items": report.feed_items,
                "feeds_ok": report.feeds_ok,
                "feeds_failed": report.feeds_failed,
                "scanned_series": report.scanned_series,
                "series_with_gaps": report.series_with_gaps,
                "total_missing": report.total_missing,
                "total_covered": report.total_covered,
                "elapsed_seconds": round(report.elapsed, 1),
                "errors": report.errors[:20],
            },
            "result": json.loads(report.render_json(item_limit=int(payload.get("item_limit") or 3))),
        }

    async def h_gaps_last(self, headers, query, body) -> dict[str, Any]:
        if self.last_gaps is None:
            return {"ok": True, "result": None, "summary": None}
        report = self.last_gaps
        return {
            "ok": True,
            "summary": {
                "feed_items": report.feed_items,
                "series_with_gaps": report.series_with_gaps,
                "total_missing": report.total_missing,
                "total_covered": report.total_covered,
                "elapsed_seconds": round(report.elapsed, 1),
            },
            "result": json.loads(report.render_json()),
        }

    async def h_feeds_check(self, headers, query, body) -> dict[str, Any]:
        """体检所有 RSS 源：能不能抓、抓到多少、最新几条是什么。

        这是配 RSS 时最该先点的按钮——不需要配 Emby/TMDB 也能用。
        """
        from .feedcheck import check_feeds

        hub = self.hub
        if not hub.settings.subscriptions:
            raise ValueError("还没有任何订阅（RSS 源），请先在「订阅」页面添加")

        payload: dict[str, Any] = {}
        try:
            payload = parse_json_body(body)
        except ValueError:
            payload = {}
        try:
            preview = int(payload.get("preview") or 5)
        except (TypeError, ValueError):
            preview = 5

        report = await check_feeds(
            hub.settings, hub.http, preview_limit=max(1, min(preview, 20))
        )
        self.last_feeds = report
        return {
            "ok": True,
            "summary": {
                "total": len(report.checks),
                "ok": report.ok_count,
                "failed": len(report.failed),
                "skipped_disabled": report.skipped_disabled,
                "elapsed_seconds": round(report.elapsed, 2),
            },
            "result": json.loads(report.render_json()),
        }

    async def h_feeds_last(self, headers, query, body) -> dict[str, Any]:
        if self.last_feeds is None:
            return {"ok": True, "result": None}
        return {"ok": True, "result": json.loads(self.last_feeds.render_json())}

    async def h_selfcheck(self, headers, query, body) -> dict[str, Any]:
        """部署自检：目录权限、订阅表、模板、服务配置。不发网络请求，秒出。"""
        from .selfcheck import run_selfcheck

        report = run_selfcheck(self.hub.settings)
        return {
            "ok": True,
            "passed": report.passed,
            "failures": len(report.failures),
            "warnings": len(report.warnings),
            "result": json.loads(report.render_json()),
            "text": report.render_text(),
        }

    async def h_preflight(self, headers, query, body) -> dict[str, Any]:
        """连通性预检：真的连一次 TG / TMDB / Emby。默认不发推送。"""
        from .selfcheck import run_preflight

        payload: dict[str, Any] = {}
        try:
            payload = parse_json_body(body)
        except ValueError:
            payload = {}

        report = await run_preflight(self.hub, notify=bool(payload.get("notify")))
        return {
            "ok": True,
            "passed": report.passed,
            "failures": len(report.failures),
            "warnings": len(report.warnings),
            "result": json.loads(report.render_json()),
            "text": report.render_text(),
        }

    async def h_tg_test(self, headers, query, body) -> dict[str, Any]:
        hub = self.hub
        if not hub.settings.telegram.enabled:
            raise ValueError("Telegram 未配置：请先填写机器人 Token 与 chat id")
        try:
            me = await hub.tg.get_me()
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"连接 Telegram 失败：{exc}") from exc

        text = (
            "🔔 <b>rss-media-hub 测试消息</b>\n"
            f"版本 v{__version__}\n"
            f"订阅数量：{len(hub.settings.subscriptions)}\n"
            f"媒体服务器：{hub.settings.library.url or '未配置'}"
        )
        sent = await hub.tg.send_message(text)
        if not sent.ok:
            raise ValueError(f"发送失败：{sent.error}")
        return {"ok": True, "bot": me.get("username"), "message": "测试消息已发送，请查看 Telegram"}

    async def h_check(self, headers, query, body) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        try:
            payload = parse_json_body(body)
        except ValueError:
            payload = {}
        target = str(payload.get("id") or "")
        subs = [rt.sub for rt in self.hub.runtimes.values() if rt.sub.enabled]
        if target:
            subs = [s for s in subs if s.id == target or s.name == target]
            if not subs:
                raise ValueError(f"找不到订阅：{target}")

        rows = []
        for sub in subs:
            result = await self.hub.reconcile_subscription(sub, notify=bool(payload.get("notify")))
            rows.append(
                {
                    "id": sub.id,
                    "name": sub.name,
                    "ok": result.ok,
                    "owned": result.owned,
                    "total": result.total,
                    "aired": result.aired,
                    "done": result.done,
                    "missing": result.missing_ranges(),
                    "error": result.error,
                }
            )
        return {"ok": True, "results": rows}

    async def h_labels_run(self, headers, query, body) -> dict[str, Any]:
        """立即执行一次 Transmission 站点标签打标（可选 --apply）。"""
        payload: dict[str, Any] = {}
        try:
            payload = parse_json_body(body)
        except ValueError:
            payload = {}
        apply = bool(payload.get("apply"))
        result = await self.hub.label_once(apply=apply)
        if not result.get("ok"):
            raise ValueError(result.get("error") or "打标失败")
        return {"ok": True, "result": result}

    async def h_labels_mappings_get(self, headers, query, body) -> dict[str, Any]:
        """读取站点标签映射表（mappings.txt）内容。"""
        path = self.hub.settings.mappings_file
        text = ""
        if path.exists():
            text = path.read_text(encoding="utf-8")
        return {"ok": True, "path": str(path), "text": text}

    async def h_labels_mappings_post(self, headers, query, body) -> dict[str, Any]:
        """保存站点标签映射表内容，并热重载。"""
        payload = parse_json_body(body)
        text = payload.get("text")
        if text is None:
            raise ValueError("缺少 text 字段（mappings.txt 的完整内容）")
        path = self.hub.settings.mappings_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        # 热重载映射表（下一次打标就用新映射）
        from .trlabeler import load_mappings as _load_mappings

        self.hub.tr_mappings = LabelMappings(_load_mappings(path))
        return {"ok": True, "path": str(path), "message": "映射表已保存"}


def _scan_summary(result: ScanResult | None) -> dict[str, Any] | None:
    if result is None:
        return None
    return {
        "generated_at": int(result.finished_at or result.started_at),
        "library_total": result.library_total,
        "scanned": result.scanned,
        "elapsed_seconds": round(result.elapsed, 1),
        "tmdb_calls": result.tmdb_calls,
        "cache_hits": result.cache_hits,
        "complete": len(result.complete),
        "partial": len(result.partial),
        "empty": len(result.empty),
        "unmatched": len(result.unmatched),
        "error": len(result.errors),
        "missing_episodes": result.total_missing_episodes,
        "top_gaps": [
            {
                "name": item.display_name,
                "owned": item.owned,
                "total": item.total,
                "missing": item.missing_ranges(),
                "missing_count": item.missing_count,
            }
            for item in sorted(result.partial, key=lambda s: -s.missing_count)[:15]
        ],
    }


