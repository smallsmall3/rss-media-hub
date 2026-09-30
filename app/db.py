"""SQLite 状态存储：RSS 条目去重、订阅完成状态、通知记录。

为什么要落盘去重：RSS 源经常整篇重发（PT 站尤其明显），只靠内存去重会在
容器重启后把历史条目重新推一遍。这里用 (subscription_id, item_key) 做唯一键，
item_key 优先取 guid，其次取下载链接，最后退回标题+发布时间。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id TEXT NOT NULL,
    item_key        TEXT NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    link            TEXT NOT NULL DEFAULT '',
    download_url    TEXT NOT NULL DEFAULT '',
    size_bytes      INTEGER,
    published_at    TEXT NOT NULL DEFAULT '',
    episode         TEXT NOT NULL DEFAULT '',
    notified        INTEGER NOT NULL DEFAULT 0,
    skip_reason     TEXT NOT NULL DEFAULT '',
    first_seen      INTEGER NOT NULL,
    UNIQUE (subscription_id, item_key)
);
CREATE INDEX IF NOT EXISTS idx_items_sub ON items (subscription_id, first_seen DESC);

CREATE TABLE IF NOT EXISTS subscriptions (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT '',
    state         TEXT NOT NULL DEFAULT 'active',   -- active | done | removed | error
    total         INTEGER NOT NULL DEFAULT 0,
    aired         INTEGER NOT NULL DEFAULT 0,
    owned         INTEGER NOT NULL DEFAULT 0,
    missing       TEXT NOT NULL DEFAULT '',
    tmdb_id       INTEGER,
    last_check    INTEGER NOT NULL DEFAULT 0,
    last_notify   INTEGER NOT NULL DEFAULT 0,
    done_at       INTEGER,
    last_error    TEXT NOT NULL DEFAULT '',
    extra         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

-- TMDB 剧集结构缓存：全库扫描时可能涉及几百部剧，
-- 同一部剧重复扫描不该反复打 TMDB 接口（会触发限流）。
CREATE TABLE IF NOT EXISTS tmdb_cache (
    key        TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    fetched_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tmdb_cache_age ON tmdb_cache (fetched_at);
"""


@dataclass
class ItemRecord:
    subscription_id: str
    item_key: str
    title: str = ""
    link: str = ""
    download_url: str = ""
    size_bytes: int | None = None
    published_at: str = ""
    episode: str = ""


@dataclass
class SubState:
    id: str
    name: str = ""
    state: str = "active"
    total: int = 0
    aired: int = 0
    owned: int = 0
    missing: str = ""
    tmdb_id: int | None = None
    last_check: int = 0
    last_notify: int = 0
    done_at: int | None = None
    last_error: str = ""
    extra: str = ""

    @property
    def progress(self) -> float:
        base = self.aired or self.total
        if not base:
            return 0.0
        return min(1.0, self.owned / base)

    @property
    def is_done(self) -> bool:
        base = self.aired or self.total
        return bool(base) and self.owned >= base


class Database:
    """异步外观 + 同步 sqlite3：所有阻塞调用都扔到线程里，避免卡事件循环。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._lock = asyncio.Lock()
        self._conn.executescript(SCHEMA)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.commit()

    # ---------------- 基础 ----------------

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    async def _run(self, fn, *args, **kwargs):
        async with self._lock:
            return await asyncio.to_thread(fn, *args, **kwargs)

    # ---------------- 条目 ----------------

    def _known_keys(self, subscription_id: str) -> set[str]:
        cur = self._conn.execute("SELECT item_key FROM items WHERE subscription_id = ?", (subscription_id,))
        return {row["item_key"] for row in cur.fetchall()}

    async def known_keys(self, subscription_id: str) -> set[str]:
        return await self._run(self._known_keys, subscription_id)

    def _add_items(self, items: Iterable[ItemRecord], notified: bool, skip_reason: str = "") -> int:
        now = int(time.time())
        rows = [
            (
                it.subscription_id,
                it.item_key,
                it.title,
                it.link,
                it.download_url,
                it.size_bytes,
                it.published_at,
                it.episode,
                1 if notified else 0,
                skip_reason,
                now,
            )
            for it in items
        ]
        if not rows:
            return 0
        cur = self._conn.executemany(
            """INSERT OR IGNORE INTO items
               (subscription_id, item_key, title, link, download_url, size_bytes,
                published_at, episode, notified, skip_reason, first_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        self._conn.commit()
        return cur.rowcount or 0

    async def add_items(self, items: Iterable[ItemRecord], notified: bool = False, skip_reason: str = "") -> int:
        return await self._run(self._add_items, list(items), notified, skip_reason)

    def _pending_keys(self, subscription_id: str) -> set[str]:
        cur = self._conn.execute(
            "SELECT item_key FROM items WHERE subscription_id = ? AND notified = 0",
            (subscription_id,),
        )
        return {row["item_key"] for row in cur.fetchall()}

    async def pending_keys(self, subscription_id: str) -> set[str]:
        """还没推送过的条目键（用于"服务停机期间更新了库"的恢复）。"""
        return await self._run(self._pending_keys, subscription_id)

    def _count_items(self, subscription_id: str) -> tuple[int, int]:
        cur = self._conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(notified),0) AS n2 FROM items WHERE subscription_id = ?",
            (subscription_id,),
        )
        row = cur.fetchone()
        return int(row["n"]), int(row["n2"])

    async def count_items(self, subscription_id: str) -> tuple[int, int]:
        """返回 (登记条目总数, 已推送数)。"""
        return await self._run(self._count_items, subscription_id)

    def _items_by_keys(self, subscription_id: str, keys: Iterable[str]) -> list[dict[str, Any]]:
        keys = list(keys)
        if not keys:
            return []
        placeholders = ",".join("?" for _ in keys)
        cur = self._conn.execute(
            f"""SELECT item_key, title, link, download_url, size_bytes, published_at, episode
                FROM items
                WHERE subscription_id = ? AND item_key IN ({placeholders})
                ORDER BY first_seen ASC, id ASC""",
            [subscription_id, *keys],
        )
        return [dict(r) for r in cur.fetchall()]

    async def items_by_keys(self, subscription_id: str, keys: Iterable[str]) -> list[dict[str, Any]]:
        return await self._run(self._items_by_keys, subscription_id, list(keys))

    def _mark_notified(self, subscription_id: str, keys: Iterable[str], reason: str = "") -> int:
        keys = list(keys)
        if not keys:
            return 0
        placeholders = ",".join("?" for _ in keys)
        cur = self._conn.execute(
            f"""UPDATE items SET notified = 1, skip_reason = ?
                WHERE subscription_id = ? AND item_key IN ({placeholders}) AND notified = 0""",
            [reason, subscription_id, *keys],
        )
        self._conn.commit()
        return cur.rowcount or 0

    async def mark_notified(self, subscription_id: str, keys: Iterable[str], reason: str = "") -> int:
        return await self._run(self._mark_notified, subscription_id, list(keys), reason)

    def _drop_items(self, subscription_id: str) -> int:
        cur = self._conn.execute("DELETE FROM items WHERE subscription_id = ?", (subscription_id,))
        self._conn.commit()
        return cur.rowcount or 0

    async def drop_items(self, subscription_id: str) -> int:
        return await self._run(self._drop_items, subscription_id)

    # ---------------- 订阅状态 ----------------

    def _get_sub(self, sub_id: str) -> SubState | None:
        cur = self._conn.execute("SELECT * FROM subscriptions WHERE id = ?", (sub_id,))
        row = cur.fetchone()
        if not row:
            return None
        return SubState(
            id=row["id"],
            name=row["name"],
            state=row["state"],
            total=row["total"],
            aired=row["aired"],
            owned=row["owned"],
            missing=row["missing"],
            tmdb_id=row["tmdb_id"],
            last_check=row["last_check"],
            last_notify=row["last_notify"],
            done_at=row["done_at"],
            last_error=row["last_error"],
            extra=row["extra"],
        )

    async def get_sub(self, sub_id: str) -> SubState | None:
        return await self._run(self._get_sub, sub_id)

    def _upsert_sub(self, st: SubState) -> None:
        self._conn.execute(
            """INSERT INTO subscriptions
               (id, name, state, total, aired, owned, missing, tmdb_id,
                last_check, last_notify, done_at, last_error, extra)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name, state=excluded.state, total=excluded.total,
                 aired=excluded.aired, owned=excluded.owned, missing=excluded.missing,
                 tmdb_id=excluded.tmdb_id, last_check=excluded.last_check,
                 last_notify=excluded.last_notify, done_at=excluded.done_at,
                 last_error=excluded.last_error, extra=excluded.extra""",
            (
                st.id,
                st.name,
                st.state,
                st.total,
                st.aired,
                st.owned,
                st.missing,
                st.tmdb_id,
                st.last_check or int(time.time()),
                st.last_notify,
                st.done_at,
                st.last_error,
                st.extra,
            ),
        )
        self._conn.commit()

    async def upsert_sub(self, st: SubState) -> None:
        await self._run(self._upsert_sub, st)

    def _all_subs(self) -> list[SubState]:
        cur = self._conn.execute("SELECT * FROM subscriptions ORDER BY name")
        out = []
        for row in cur.fetchall():
            out.append(
                SubState(
                    id=row["id"],
                    name=row["name"],
                    state=row["state"],
                    total=row["total"],
                    aired=row["aired"],
                    owned=row["owned"],
                    missing=row["missing"],
                    tmdb_id=row["tmdb_id"],
                    last_check=row["last_check"],
                    last_notify=row["last_notify"],
                    done_at=row["done_at"],
                    last_error=row["last_error"],
                    extra=row["extra"],
                )
            )
        return out

    async def all_subs(self) -> list[SubState]:
        return await self._run(self._all_subs)

    def _delete_sub(self, sub_id: str) -> None:
        self._conn.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
        self._conn.commit()

    async def delete_sub(self, sub_id: str) -> None:
        await self._run(self._delete_sub, sub_id)

    # ---------------- KV ----------------

    def _get_kv(self, key: str) -> str | None:
        cur = self._conn.execute("SELECT v FROM kv WHERE k = ?", (key,))
        row = cur.fetchone()
        return row["v"] if row else None

    async def get_kv(self, key: str) -> str | None:
        return await self._run(self._get_kv, key)

    def _set_kv(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (key, value),
        )
        self._conn.commit()

    async def set_kv(self, key: str, value: str) -> None:
        await self._run(self._set_kv, key, value)

    # ---------------- TMDB 缓存 ----------------

    def _get_tmdb_cache(self, key: str, max_age: int) -> str | None:
        cur = self._conn.execute(
            "SELECT payload, fetched_at FROM tmdb_cache WHERE key = ?", (key,)
        )
        row = cur.fetchone()
        if not row:
            return None
        if max_age > 0 and int(time.time()) - int(row["fetched_at"]) > max_age:
            return None  # 过期当作没有，交给上层重新拉取
        return str(row["payload"])

    async def get_tmdb_cache(self, key: str, max_age: int = 0) -> str | None:
        """max_age<=0 表示只要存在就返回（不过期）。"""
        return await self._run(self._get_tmdb_cache, key, max_age)

    def _set_tmdb_cache(self, key: str, payload: str) -> None:
        self._conn.execute(
            """INSERT INTO tmdb_cache (key, payload, fetched_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET payload = excluded.payload,
                                              fetched_at = excluded.fetched_at""",
            (key, payload, int(time.time())),
        )
        self._conn.commit()

    async def set_tmdb_cache(self, key: str, payload: str) -> None:
        await self._run(self._set_tmdb_cache, key, payload)

    def _purge_tmdb_cache(self, max_age: int) -> int:
        if max_age <= 0:
            cur = self._conn.execute("DELETE FROM tmdb_cache")
        else:
            cur = self._conn.execute(
                "DELETE FROM tmdb_cache WHERE fetched_at < ?", (int(time.time()) - max_age,)
            )
        self._conn.commit()
        return cur.rowcount or 0

    async def purge_tmdb_cache(self, max_age: int = 0) -> int:
        """清理过期缓存；max_age=0 表示全清。"""
        return await self._run(self._purge_tmdb_cache, max_age)

    def _count_tmdb_cache(self) -> int:
        cur = self._conn.execute("SELECT COUNT(*) AS n FROM tmdb_cache")
        return int(cur.fetchone()["n"])

    async def count_tmdb_cache(self) -> int:
        return await self._run(self._count_tmdb_cache)
