"""编排层：RSS 轮询 → 入库比对 → Telegram 推送 → 追完退订。

运行模型（单进程，纯 asyncio）：
  * 每个订阅一个独立的上次轮询时间，到点就抓 RSS（错峰，不会同时打满 PT 站）
  * 后台巡检循环每 reconcile_interval 秒对全部订阅查一次 TMDB + 媒体库
  * 巡检发现"媒体库多了几集" → 推进入库通知；全部入库 → 推送完成并删订阅
  * 任何一次发现新种都会立刻触发一次该订阅的巡检（因为新种常常已经下载完入库了）
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Coroutine

import httpx

from . import __version__
from .config import ConfigError, Settings, Subscription, load_settings, save_subscriptions
from .db import Database, ItemRecord, SubState
from .emby import EmbyClient, EmbyError
from .notify import ItemView, Notifier
from .poster import PosterResolver, normalize_for_compare
from .feedcheck import check_feeds, FeedsReport
from .gapfill import GapFinder, GapReport
from .libraryscan import LibraryScanner, ScanResult
from .reconcile import ReconcileResult, Reconciler
from .rss import FeedItem, fetch_feed
from .telegram import TelegramSender
from .tmdb import TmdbClient, _year_of

log = logging.getLogger("rss-media-hub")


@dataclass
class HubStats:
    started_at: float = 0.0
    polls: int = 0
    feeds_ok: int = 0
    feeds_failed: int = 0
    items_seen: int = 0
    notifications: int = 0
    reconcile_runs: int = 0
    removals: int = 0
    last_error: str = ""
    last_poll_at: float = 0.0
    last_reconcile_at: float = 0.0
    sub_next_poll: dict[str, float] = field(default_factory=dict)
    relayouts: int = 0


@dataclass
class SubRuntime:
    sub: Subscription
    next_poll: float = 0.0
    last_check: float = 0.0
    last_error: str = ""


class Hub:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db = Database(settings.db_file)
        self.runtimes: dict[str, SubRuntime] = {}
        self.stats = HubStats()
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[Any]] = []
        self._wake = asyncio.Event()
        self._reconcile_only: set[str] = set()
        self._lock = asyncio.Lock()
        self._subs_mtime: float = 0.0
        self._build_services()
        self._build_runtimes()

    def _build_services(self) -> None:
        """按当前 settings 构造各个客户端。

        Web UI 保存配置后会调用 apply_settings() 重新走这里，
        这样换 Token / 换 Emby 地址不需要重启容器。
        """
        settings = self.settings
        # RSS 抓取：PT 站一般在公网可直连，所以只在配了全局代理时才走代理
        rss_kwargs: dict[str, Any] = {}
        global_proxy = (os.environ.get("RMH_PROXY") or "").strip()
        if global_proxy:
            rss_kwargs["proxy"] = global_proxy
        self.http = httpx.AsyncClient(
            timeout=30.0,
            headers={"User-Agent": "rss-media-hub/1.0 (+https://github.com/local/rss-media-hub)"},
            follow_redirects=True,
            **rss_kwargs,
        )
        self.tmdb = TmdbClient(
            settings.tmdb.api_key,
            api_base=settings.tmdb.api_base,
            language=settings.tmdb.language,
            image_base=settings.tmdb.image_base,
            timeout=settings.library.timeout,
            proxy=settings.tmdb.proxy,
        )
        self.library = EmbyClient(
            settings.library.url,
            settings.library.api_key,
            user_id=settings.library.user_id,
            kind=settings.library.kind,
            verify_tls=settings.library.verify_tls,
            timeout=settings.library.timeout,
            proxy=settings.library.proxy,
        )
        self.tg = TelegramSender(
            settings.telegram.bot_token,
            settings.telegram.chat_id,
            thread_id=settings.telegram.thread_id,
            api_base=settings.telegram.api_base,
            proxy=settings.telegram.proxy,
            disable_notification=settings.telegram.disable_notification,
        )
        self.reconciler = Reconciler(self.tmdb, self.library, settings.library)
        self.scanner = LibraryScanner(
            self.tmdb,
            self.library,
            settings.library,
            db=self.db,
            cache_ttl=settings.scan_cache_ttl,
            concurrency=settings.scan_concurrency,
        )
        self.notifier = Notifier(settings, self.tg, self.library)
        self.gapfinder = GapFinder(settings, self.http)
        # feed 模式的海报：按片名去 TMDB 搜。
        # 需要 TMDB 可用，且用户没关掉这个开关。
        self.poster_resolver = PosterResolver(
            self.tmdb,
            enabled=bool(settings.telegram.enabled and settings.tmdb.enabled and settings.telegram.feed_poster),
            image_base=settings.tmdb.image_base,
            proxy=settings.tmdb.proxy,
        )
        # 网页 UI 实例，由 _web_server() 挂上；CLI 里可能为 None
        self.web: Any = None
        # 后台附加任务（「已添加订阅」通知等），持引用防止被 GC 掉
        self._bg: set[asyncio.Task[Any]] = set()

    async def apply_settings(self, new_settings: Settings) -> None:
        """热重载配置：按新配置重建全部组件，然后关掉旧客户端。

        注意顺序：**先建新的再关旧的**。反过来的话，重建期间如果有请求
        正在用旧客户端，会撞上"operation on closed client"。
        """
        old = (self.http, self.tmdb, self.library, self.tg)
        self.settings = new_settings
        self._build_services()          # 里面已经把 notifier 一起重建了
        for client in old:
            with contextlib.suppress(Exception):
                await client.aclose()
        self._rebuild_runtimes_after_reload()

    def reload_subscriptions_file(self) -> bool:
        """重新读 subscriptions.yaml（网页 UI 加/删订阅后调用）。"""
        try:
            subs = load_settings(self.settings.config_dir, self.settings.state_dir).subscriptions
        except ConfigError as exc:
            log.error("subscriptions.yaml 解析失败：%s", exc)
            return False
        self.settings.subscriptions = subs
        self._rebuild_runtimes_after_reload()
        try:
            self._subs_mtime = self.settings.subs_file.stat().st_mtime
        except OSError:
            self._subs_mtime = 0.0
        return True

    def _rebuild_runtimes_after_reload(self) -> None:
        """保留已有订阅的轮询计划，新增的补上，删除的丢掉。"""
        import time

        now = time.time()
        old = self.runtimes
        new_map: dict[str, SubRuntime] = {}
        for idx, sub in enumerate(self.settings.subscriptions):
            rt = old.get(sub.id)
            if rt is not None:
                rt.sub = sub
            else:
                rt = SubRuntime(sub=sub, next_poll=now + idx * 2.0, last_check=0.0)
                log.info("新增订阅：%s", sub.name)
            new_map[sub.id] = rt
        for sid in set(old) - set(new_map):
            log.info("订阅已移除：%s", old[sid].sub.name)
        self.runtimes = new_map

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def __aenter__(self) -> "Hub":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self.tg.aclose()
        await self.library.aclose()
        await self.tmdb.aclose()
        await self.poster_resolver.aclose()
        await self.http.aclose()
        self.db.close()

    def _build_runtimes(self) -> None:
        import time

        now = time.time()
        interval = max(60, self.settings.poll_interval)
        self.runtimes = {}
        for idx, sub in enumerate(self.settings.subscriptions):
            # 错峰：把每个订阅的第一次轮询摊到整个周期里
            offset = (idx / max(1, len(self.settings.subscriptions))) * interval
            self.runtimes[sub.id] = SubRuntime(sub=sub, next_poll=now + offset, last_check=0.0)
        log.info("已加载 %d 条订阅", len(self.runtimes))

    async def reload_subscriptions(self) -> bool:
        """检测 subscriptions.yaml 变化并热重载（不重启容器）。"""
        path = self.settings.subs_file
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return False
        if mtime == self._subs_mtime:
            return False
        self._subs_mtime = mtime
        try:
            subs = load_settings(self.settings.config_dir, self.settings.state_dir).subscriptions
        except ConfigError as exc:
            log.error("subscriptions.yaml 解析失败，继续用旧配置：%s", exc)
            return False

        self.settings.subscriptions = subs
        self._rebuild_runtimes_after_reload()
        self.stats.relayouts += 1
        return True

    # ------------------------------------------------------------------
    # 订阅添加通知
    # ------------------------------------------------------------------
    def spawn_bg(self, coro: Coroutine[Any, Any, Any]) -> None:
        """后台执行附加任务：不阻塞调用方，异常由任务内部自行消化。"""
        task = asyncio.get_running_loop().create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def announce_subscription(self, sub: Subscription) -> None:
        """新订阅保存后，往 Telegram 发一条「已添加订阅」确认（尽量带海报）。

        形如：风华令 (2026) S01 已添加订阅
          * 有 tmdb_id → 直取 TMDB 条目：中文名 + 年份 + 海报
          * 只有订阅名（feed 模式）→ 名字是用户随手起的标签，
            只有**精确**命中 TMDB 才配海报，否则会配出不相干的图
          * TMDB 未配置 / 没命中 → 只发文字
        全程吞异常：订阅本身已经保存成功，通知只是锦上添花。
        """
        try:
            if not self.tg.enabled:
                log.info("Telegram 未配置，跳过「已添加订阅」通知：%s", sub.name)
                return
            title, year, poster = sub.name, None, None
            if self.settings.tmdb.enabled:
                try:
                    raw = await self._tmdb_raw_for_announce(sub)
                    if raw is not None:
                        title = str(raw.get("name") or "") or sub.name
                        year = _year_of(raw.get("first_air_date"))
                        poster = await self.poster_resolver.download_poster(
                            str(raw.get("poster_path") or "")
                        )
                except Exception as exc:  # noqa: BLE001
                    log.debug("「已添加订阅」查 TMDB/海报失败，改发纯文字：%s", exc)
            caption = self.notifier.render_sub_added(sub, title=title, year=year)
            if poster:
                sent = await self.tg.send_photo(poster, caption=caption)
                if sent.ok:
                    return
                log.debug("订阅海报发送失败，改发文字：%s", sent.error)
            await self.tg.send_message(caption)
        except Exception as exc:  # noqa: BLE001
            log.warning("「已添加订阅」通知没发出去（不影响订阅本身）：%s", exc)

    async def _tmdb_raw_for_announce(self, sub: Subscription) -> dict[str, Any] | None:
        """为「已添加订阅」通知查 TMDB 条目；返回原始 tv 对象或 None。"""
        if sub.tmdb_id:
            return await self.tmdb.resolve(sub.name, sub.tmdb_id)
        # 没有_tmdb_id：订阅名精确命中才采用（宁缺勿错）
        results = await self.tmdb.search_tv(sub.name)
        target = normalize_for_compare(sub.name)
        for item in results or []:
            for candidate in (item.get("name"), item.get("original_name")):
                if candidate and normalize_for_compare(str(candidate)) == target:
                    return item
        return None

    async def stop(self, *_args: Any) -> None:
        if not self._stop.is_set():
            log.info("收到停止信号，正在优雅退出…")
        self._stop.set()
        self._wake.set()

    # ------------------------------------------------------------------
    # 启动前自检
    # ------------------------------------------------------------------
    async def preflight(self) -> None:
        problems: list[str] = []
        if not self.settings.telegram.enabled:
            problems.append("Telegram 未配置（RMH_TG_BOT_TOKEN / RMH_TG_CHAT_ID）")
        else:
            try:
                me = await self.tg.get_me()
                log.info("Telegram 机器人：@%s", me.get("username"))
            except Exception as exc:  # noqa: BLE001
                problems.append(f"Telegram 连接失败：{exc}")
        if not self.settings.tmdb.enabled:
            problems.append("TMDB 未配置（RMH_TMDB_API_KEY）")
        if not self.settings.library.enabled:
            problems.append("Emby/Jellyfin 未配置（RMH_EMBY_URL / RMH_EMBY_API_KEY）")
        else:
            try:
                info = await self.library.ping()
                kind = await self.library.detect_kind()
                log.info("媒体服务器：%s %s（%s）", kind, info.get("Version"), self.settings.library.url)
                await self.library.resolve_user_id()
            except Exception as exc:  # noqa: BLE001
                problems.append(f"媒体服务器连接失败：{exc}")
        if not self.runtimes:
            problems.append("没有任何订阅：请编辑 config/subscriptions.yaml 后重启")

        for problem in problems:
            log.warning("⚠️  %s", problem)

    # ------------------------------------------------------------------
    # RSS 轮询
    # ------------------------------------------------------------------
    async def poll_subscription(self, rt: SubRuntime) -> None:
        sub = rt.sub
        urls = sub.rss_urls
        if not sub.enabled or not urls:
            return
        collected: list[FeedItem] = []
        for url in urls:
            try:
                items = await fetch_feed(self.http, url, retries=3, timeout=30.0)
                self.stats.feeds_ok += 1
            except Exception as exc:  # noqa: BLE001
                self.stats.feeds_failed += 1
                rt.last_error = f"RSS 抓取失败：{exc}"
                log.warning("[%s] RSS 抓取失败 %s：%s", sub.name, _mask(url), exc)
                continue
            collected.extend(items)

        if not collected:
            return

        self.stats.items_seen += len(collected)
        known = await self.db.known_keys(sub.id)
        first_run = not known
        fresh = [it for it in collected if it.key not in known]
        if not fresh:
            return

        # 按时间升序处理，保证推送顺序自然
        fresh.sort(key=lambda it: (it.published.timestamp() if it.published else 0.0))

        to_notify: list[FeedItem] = []
        skipped: list[tuple[FeedItem, str]] = []
        for item in fresh:
            if not item.title:
                skipped.append((item, "空标题"))
                continue
            allowed, reason = sub.allows_title(item.title)
            if allowed:
                to_notify.append(item)
            else:
                skipped.append((item, reason))

        records = [_to_record(sub.id, it) for it in fresh]

        # 首轮要不要静默：feed 模式默认"补推历史"（用户要的就是全量），
        # show 模式默认静默登记（避免一上线被几百条历史刷屏）。
        # 订阅自己写了 seed 就以订阅为准；都没写时退回全局 RMH_SEED_SILENT。
        should_seed = sub.seed if sub.seed is not None else self.settings.seed_silent
        if first_run and should_seed:
            await self.db.add_items(records, notified=True, skip_reason="首轮登记，未推送")
            log.info("[%s] 首轮登记 %d 条 RSS 条目（静默，不推送）", sub.name, len(records))
            await self._touch_sub_state(sub, note=f"已登记 {len(records)} 条历史条目")
            return

        rest = [it for it in fresh if it not in to_notify]
        if rest:
            await self.db.add_items([_to_record(sub.id, it) for it in rest], notified=True, skip_reason="被订阅规则过滤")
        notified_keys: list[str] = []
        if to_notify:
            to_notify = to_notify[-max(1, self.settings.max_items_per_poll):]
            await self.db.add_items([_to_record(sub.id, it) for it in to_notify], notified=False)
            notified_keys = [it.key for it in to_notify]

            # show 模式：先做一次比对，这样新种通知里能带上"入库 3/12 集"
            # feed 模式：不比对媒体库，纯转发（也不需要配 TMDB/Emby）
            result = None
            if sub.is_show:
                result = await self._safe_reconcile(sub)
            views = [ItemView.from_feed(it) for it in to_notify]
            if sub.is_feed:
                text = self.notifier.render_feed_items(sub, views)
            else:
                text = self.notifier.render_new_items(sub, views, result, source="PT RSS")

            poster = (sub.name, result) if sub.is_show else None
            feed_posters = None
            if sub.is_feed:
                # feed 模式：给第一条配一张它自己的海报（按片名搜 TMDB）
                feed_posters = await self.notifier.poster_items(
                    sub, views, resolver=self.poster_resolver
                ) or None
            ok = await self.notifier.push(text, poster=poster, poster_items=feed_posters)
            if ok:
                self.stats.notifications += 1
                await self.db.mark_notified(sub.id, notified_keys, reason="已推送新种")
            else:
                log.warning("[%s] 新种推送失败，下次轮询重试", sub.name)
                await self.db.add_items([_to_record(sub.id, it) for it in to_notify], notified=False)

        if skipped:
            detail = "；".join(f"{it.title[:40]}…（{reason}）" for it, reason in skipped[:3])
            log.info("[%s] 按订阅规则过滤 %d 条：%s", sub.name, len(skipped), detail)

        if notified_keys and sub.is_show:
            # 新种刚出现，媒体库很可能同步就有了，立刻巡检一次
            self._reconcile_only.add(sub.id)
            self._wake.set()

    async def _touch_sub_state(self, sub: Subscription, note: str = "") -> None:
        """记录"首轮登记了 N 条历史条目"这类备注，供 `list` 命令查看。"""
        state = await self.db.get_sub(sub.id)
        if state is None:
            state = SubState(id=sub.id, name=sub.name)
        state.name = sub.name
        if note:
            stamp = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M")
            state.extra = f"{note}（{stamp}）"
        await self.db.upsert_sub(state)

    # ------------------------------------------------------------------
    # 巡检（比对）
    # ------------------------------------------------------------------
    async def reconcile_subscription(self, sub: Subscription, *, notify: bool = True) -> ReconcileResult:
        # feed 模式不追踪单部剧，没有"入库进度"这个概念，直接跳过比对
        if sub.is_feed:
            result = ReconcileResult(subscription_id=sub.id)
            result.error = "feed 模式不做入库比对（RSS 全量转发）"
            if notify:
                log.debug("[%s] feed 模式跳过巡检", sub.name)
            return result

        previous = await self.db.get_sub(sub.id)
        result = await self.reconciler.reconcile(sub, previous)
        self.stats.reconcile_runs += 1
        self.stats.last_reconcile_at = result.checked_at

        if not result.ok:
            state = previous or SubState(id=sub.id, name=sub.name)
            state.name = sub.name
            state.last_error = result.error
            state.last_check = result.checked_at
            state.state = "error"
            await self.db.upsert_sub(state)
            log.warning("[%s] 巡检失败：%s", sub.name, result.error)
            return result

        state = result.as_state(sub, previous)
        await self.db.upsert_sub(state)
        log.info(
            "[%s] 入库 %d/%d（已播出 %d）%s",
            sub.name,
            result.owned,
            result.total,
            result.aired,
            "｜缺 " + result.missing_ranges() if result.missing else "｜已完整",
        )

        if not notify:
            return result

        # ---- 1) 媒体库新增了集数 → 推入库通知 ----
        if result.new_codes:
            codes = result.new_codes
            views: list[ItemView] = []
            matched: list[str] = []
            for code in codes:
                rows = await self._find_items_for_code(sub.id, code)
                for row in rows:
                    views.append(ItemView.from_row(row))
                    matched.append(row["item_key"])
            text = self.notifier.render_library_update(sub, result, codes, views)
            if await self.notifier.push(text, poster=(sub.name, result)):
                self.stats.notifications += 1
                await self.db.mark_notified(sub.id, matched, reason="本地入库，无需再推送资源")
            else:
                log.warning("[%s] 入库通知推送失败", sub.name)

        # ---- 2) 全部入库 → 推完成通知 + 删订阅 ----
        if result.done and not (previous and previous.state == "done"):
            text = self.notifier.render_done(sub, result, removed=sub.remove_when_done)
            if await self.notifier.push(text, poster=(sub.name, result)):
                self.stats.notifications += 1
            if sub.remove_when_done:
                await self.remove_subscription(sub, reason="已全部入库")
            log.info("[%s] 订阅完成%s", sub.name, "，已删除订阅" if sub.remove_when_done else "")

        return result

    async def _find_items_for_code(self, sub_id: str, code: str) -> list[dict[str, Any]]:
        """把 S01E02 这种集号映射回还没推送的 RSS 条目。"""
        pending = await self.db.pending_keys(sub_id)
        if not pending:
            return []
        rows = await self.db.items_by_keys(sub_id, pending)
        want = code.upper()
        bare = want.split("E")[-1].lstrip("0") if "E" in want else want
        hit: list[dict[str, Any]] = []
        for row in rows:
            label = str(row.get("episode") or "").upper()
            if not label:
                continue
            if want in label or label in want:
                hit.append(row)
                continue
            if bare and label.endswith(f"E{bare.zfill(2)}"):
                hit.append(row)
        return hit

    async def _safe_reconcile(self, sub: Subscription) -> ReconcileResult | None:
        try:
            return await self.reconcile_subscription(sub, notify=False)
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] 快速比对失败：%s", sub.name, exc)
            return None

    # ------------------------------------------------------------------
    # 订阅管理
    # ------------------------------------------------------------------
    async def remove_subscription(self, sub: Subscription, reason: str = "") -> None:
        async with self._lock:
            current = self.settings.subscriptions
            remaining = [s for s in current if s.id != sub.id]
            if len(remaining) == len(current):
                return
            record = await self.db.get_sub(sub.id)
            state = record or SubState(id=sub.id, name=sub.name)
            state.state = "removed"
            state.name = sub.name
            await self.db.upsert_sub(state)
            try:
                save_subscriptions(self.settings.subs_file, remaining)
            except Exception as exc:  # noqa: BLE001
                log.error("写回 subscriptions.yaml 失败：%s", exc)
                return
            self.settings.subscriptions = remaining
            self.runtimes.pop(sub.id, None)
            self.stats.removals += 1
            try:
                self._subs_mtime = self.settings.subs_file.stat().st_mtime
            except OSError:
                self._subs_mtime = 0.0
            log.info("已删除订阅 %s（%s），剩余 %d 条", sub.name, reason, len(remaining))

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    async def start(self, *, probe: bool = True) -> None:
        """启动流程：预检 + 加载订阅 + 起三个后台循环。

        和 run() 分开是有意的：smoke 测试只需要"能不能正常启动"，
        不需要一直跑到有人按 Ctrl+C。

        probe=False 时跳过所有会发网络请求的探测（连通性预检、首轮 RSS 抓取）。
        CI 里必须这么用：api.telegram.org 在某些 runner 上会被卡住不放，
        导致收尾阶段一直等，整个 job 拖到一分钟以上。
        """
        import time

        self.stats.started_at = time.time()
        log.info("rss-media-hub v%s 启动，轮询 %ds / 巡检 %ds",
                 __version__, self.settings.poll_interval, self.settings.reconcile_interval)
        if probe:
            await self.preflight()
        await self.reload_subscriptions()

        self._tasks = [
            asyncio.create_task(self._poll_loop(probe=probe), name="poll"),
            asyncio.create_task(self._reconcile_loop(), name="reconcile"),
            asyncio.create_task(self._watch_loop(), name="watch"),
        ]

    async def run(self) -> None:
        """常驻运行：启动后一直等到 stop() 被调用。"""
        await self.start()
        tasks = self._tasks
        try:
            await self._stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        log.info(
            "退出统计：轮询 %d 次 / 成功源 %d / 失败源 %d / 推送 %d 条 / 退订 %d 条",
            self.stats.polls, self.stats.feeds_ok, self.stats.feeds_failed,
            self.stats.notifications, self.stats.removals,
        )

    async def _poll_loop(self, *, probe: bool = True) -> None:
        """RSS 轮询循环。

        probe=False 时把首次抓取推迟一个周期：CI 的冒烟测试只关心
        "服务和循环能不能起来"，不想在假 RSS 地址上耗时间。
        注意不能用 return —— 那样循环就没了，watch 之类也观察不到它。
        """
        import time

        if not probe:
            await self._sleep_until_wake(2.0)

        while not self._stop.is_set():
            now = time.time()
            due: list[SubRuntime] = []
            interval = self.poll_interval_seconds()
            for rt in list(self.runtimes.values()):
                if rt.next_poll <= now:
                    due.append(rt)
                    rt.next_poll = now + self._next_poll_delay(interval)

            for rt in due:
                if self._stop.is_set():
                    break
                try:
                    await self.poll_subscription(rt)
                except Exception as exc:  # noqa: BLE001
                    rt.last_error = str(exc)
                    self.stats.last_error = str(exc)
                    log.exception("[%s] 轮询异常：%s", rt.sub.name, exc)
            if due:
                self.stats.polls += 1
                self.stats.last_poll_at = time.time()

            await self._sleep_until_wake(5.0)

    def poll_interval_seconds(self) -> int:
        """RSS 刷新间隔（秒）。下限 60 秒，避免把 PT 站打爆。"""
        return max(60, int(self.settings.poll_interval))

    def _next_poll_delay(self, interval: int) -> float:
        """下一次轮询的延迟：一个周期 + 最多 10% 抖动（防止所有订阅同时打站）。"""
        return interval + random.uniform(0, interval * 0.1)

    async def _reconcile_loop(self) -> None:
        import time

        # 启动后先等一轮，让 RSS 首轮登记先完成
        await self._sleep_until_wake(10.0)
        while not self._stop.is_set():
            subs = [
                rt.sub
                for rt in list(self.runtimes.values())
                if rt.sub.enabled and rt.sub.is_show  # feed 模式没有入库进度可巡检
            ]
            for sub in subs:
                if self._stop.is_set():
                    break
                try:
                    await self.reconcile_subscription(sub, notify=True)
                except Exception as exc:  # noqa: BLE001
                    self.stats.last_error = str(exc)
                    log.exception("[%s] 巡检异常：%s", sub.name, exc)
            self._reconcile_only.clear()
            self.stats.last_reconcile_at = time.time()
            interval = max(120, self.settings.reconcile_interval)
            await self._sleep_until_wake(interval)

            # 被新种事件唤醒时，只巡检相关订阅
            if self._reconcile_only and not self._stop.is_set():
                targets = [self.runtimes[sid] for sid in list(self._reconcile_only) if sid in self.runtimes]
                self._reconcile_only.clear()
                await asyncio.sleep(45)  # 给下载/入库留点时间
                for rt in targets:
                    try:
                        await self.reconcile_subscription(rt.sub, notify=True)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("[%s] 触发式巡检失败：%s", rt.sub.name, exc)

    async def _watch_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.reload_subscriptions()
            except Exception as exc:  # noqa: BLE001
                log.warning("热重载订阅失败：%s", exc)
            await self._sleep_until_wake(30.0)

    async def _sleep_until_wake(self, seconds: float) -> None:
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------


def _to_record(sub_id: str, item: FeedItem) -> ItemRecord:
    return ItemRecord(
        subscription_id=sub_id,
        item_key=item.key,
        title=item.title,
        link=item.link,
        download_url=item.download_url,
        size_bytes=item.size_bytes,
        published_at=item.published.isoformat() if item.published else item.published_raw,
        episode=item.episode_label,
    )


def _mask(url: str) -> str:
    """日志里隐藏 passkey，避免密钥进日志文件。"""
    import re

    return re.sub(r"(passkey|passphrase|torrent_pass|api_key|apikey)=([^&/\s]+)", r"\1=***", url, flags=re.IGNORECASE)


def _display_width(text: str) -> int:
    """按终端显示宽度算长度（中文/全角算 2 列），这样表格能对齐。"""
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(ch) in {"W", "F"} else 1 for ch in text)


def _clip(text: str, width: int) -> str:
    """按显示宽度截断并补空格。"""
    if _display_width(text) <= width:
        return text + " " * (width - _display_width(text))
    out = ""
    used = 0
    for ch in text:
        w = 2 if __import__("unicodedata").east_asian_width(ch) in {"W", "F"} else 1
        if used + w > width - 1:
            break
        out += ch
        used += w
    return out + "…" + " " * max(0, width - used - 1)


# --------------------------------------------------------------------------
# CLI 命令
# --------------------------------------------------------------------------


async def cmd_run(settings: Settings) -> int:
    async with Hub(settings) as hub:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda: asyncio.create_task(hub.stop()))
            except (NotImplementedError, AttributeError):  # pragma: no cover - Windows
                pass
        health = asyncio.create_task(_web_server(hub))
        try:
            await hub.run()
        finally:
            health.cancel()
            await asyncio.gather(health, return_exceptions=True)
    return 0


async def _web_server(hub: Hub) -> None:
    """网页 UI + /healthz + /status，一个服务全包（见 webserve.py）。"""
    from .webserve import WebUI

    if not hub.settings.ui_enabled:
        log.info("网页 UI 已通过 RMH_UI_ENABLED=false 关闭，仅保留 /healthz")
    ui = WebUI(
        hub,
        port=hub.settings.health_port,
        token=hub.settings.ui_token,
    )
    hub.web = ui  # 方便测试与后续扩展
    if not hub.settings.ui_enabled:
        ui._routes = [r for r in ui._routes if r[1] in {"/healthz", "/api/status"}]
    await ui.serve_forever()


async def cmd_web(settings: Settings) -> int:
    """只跑网页 UI（不启动轮询/巡检循环），用于调试界面。"""
    async with Hub(settings) as hub:
        print(f"网页 UI：http://127.0.0.1:{settings.health_port}/")
        if settings.ui_token:
            print("已启用访问口令（RMH_UI_TOKEN）")
        else:
            print("未设置 RMH_UI_TOKEN，同一局域网内任何人都能打开并改配置")
        await _web_server(hub)
    return 0


async def cmd_check(settings: Settings, only: str | None = None, notify: bool = False) -> int:
    """跑一次完整比对并把结果打到终端（不推送，除非 --notify）。"""
    async with Hub(settings) as hub:
        if not hub.runtimes:
            print("没有任何订阅，请先编辑 config/subscriptions.yaml")
            return 1
        subs = [rt.sub for rt in hub.runtimes.values() if rt.sub.enabled]
        if only:
            subs = [s for s in subs if s.id == only or s.name == only]
            if not subs:
                print(f"找不到订阅：{only}")
                return 1
        header = "  ".join(
            (_clip("订阅", 26), _clip("入库/总", 10), _clip("已播出", 8), "状态 / 缺失")
        )
        print(header)
        print("-" * 84)
        failed = 0
        for sub in subs:
            result = await hub.reconcile_subscription(sub, notify=notify)
            name_cell = _clip(sub.name, 26)
            if not result.ok:
                failed += 1
                print(f"{name_cell}  {_clip('-', 10)}  {_clip('-', 8)}  ❌ {result.error}")
                continue
            counts = _clip(f"{result.owned}/{result.total}", 10)
            aired = _clip(str(result.aired), 8)
            status = "✅ 已完成" if result.done else ("⏳ 追更中" if result.owned else "🕐 未入库")
            print(
                f"{name_cell}  {counts}  {aired}  {status}"
                + (f"｜缺 {result.missing_ranges()}" if result.missing else "")
            )
        return 0 if failed == 0 else 2


async def cmd_scan(
    settings: Settings,
    *,
    limit: int | None = None,
    json_only: bool = False,
    export: str | None = None,
    show_complete: bool = False,
    top: int = 15,
    refresh: bool = False,
) -> int:
    """扫描整个媒体库，列出每部剧的「入库 x / 全部 y」，并给出缺集报告。

    这是「以媒体库为订阅源」的第一步：先摸清库里到底缺什么。
    """
    import json as _json
    import sys as _sys
    from pathlib import Path as _Path

    # --json 模式下 stdout 只留纯 JSON，提示信息一律走 stderr，
    # 这样 `python -m app scan --json | jq` 才能直接用。
    def info(message: str) -> None:
        print(message, file=_sys.stderr if json_only else _sys.stdout)

    async with Hub(settings) as hub:
        if not settings.library.enabled:
            print("❌ 没有配置 Emby/Jellyfin（RMH_EMBY_URL / RMH_EMBY_API_KEY），无法扫描")
            return 1
        if not settings.tmdb.enabled:
            print("❌ 没有配置 TMDB API Key，无法比对集数")
            return 1

        if refresh:
            purged = await hub.db.purge_tmdb_cache(settings.scan_cache_ttl)
            info(f"已清理过期 TMDB 缓存 {purged} 条")

        max_series = settings.scan_max_series if limit is None else limit
        if max_series:
            info(f"将最多检查 {max_series} 部剧（RMH_SCAN_MAX_SERIES，0 表示不限）")

        state = {"last": 0.0}

        def on_progress(done: int, total: int, scan) -> None:
            import time as _time

            now = _time.time()
            # 最多每 0.5 秒刷一行，避免上百部剧时刷屏
            if now - state["last"] < 0.5 and done != total:
                return
            state["last"] = now
            tail = "" if scan is None else f"  {scan.display_name[:28]:<28} {scan.owned}/{scan.total}"
            info(f"  扫描进度 {done}/{total}{tail}")

        info("开始扫描媒体库…")
        result: ScanResult = await hub.scanner.scan(
            limit=max_series,
            on_progress=on_progress,
        )

        if json_only:
            print(result.render_json())
        else:
            print()
            print(result.render_text(top=top, show_complete=show_complete))

        if export:
            path = _Path(export)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(result.render_json(), encoding="utf-8")
            info(f"完整报告（JSON）已写入：{path}")

        return 0


async def cmd_smoke(settings: Settings) -> int:
    """冒烟测试：真的把服务和网页 UI 起一遍，确认能正常启动然后自动退出。

    为什么需要它：`run` 是常驻循环，CI 里跑到那里会一直卡到超时；
    而只跑 `--version` 又验证不了启动路径。这个命令两者兼顾：
    走完整启动流程（预检 + 加载订阅 + 起循环 + 起网页服务），
    确认 /healthz 有响应，然后自己停下。
    """
    from .webserve import WebUI

    ok = True
    async with Hub(settings) as hub:
        # probe=False：不发任何网络请求。CI 里 api.telegram.org 可能被卡住，
        # 会让这个 job 从 2 秒拖到一分钟以上。
        await hub.start(probe=False)
        log.info("smoke: 启动流程完成，订阅 %d 条", len(hub.runtimes))

        # 用端口 0 让系统分配，避免 CI 里端口被占
        ui = WebUI(hub, host="127.0.0.1", port=0, token=settings.ui_token)
        hub.web = ui
        server = asyncio.create_task(ui.serve_forever())
        try:
            for _ in range(50):  # 最多等 5 秒
                if ui.bound_port:
                    break
                await asyncio.sleep(0.1)
            port = ui.bound_port
            if not port:
                print("❌ smoke: 网页服务没起来", file=sys.stderr)
                return 1

            import httpx

            # trust_env=False：不要读 HTTP_PROXY 之类环境变量。
            # 这是本机回环请求，走代理会得到 502。
            async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
                health = await client.get(f"http://127.0.0.1:{port}/healthz")
                if health.status_code != 200 or health.text.strip() != "ok":
                    print(f"❌ smoke: /healthz 异常 {health.status_code} {health.text[:80]}", file=sys.stderr)
                    ok = False
                else:
                    print(f"✅ smoke: /healthz 正常（127.0.0.1:{port}）")

                status = await client.get(f"http://127.0.0.1:{port}/api/status")
                if status.status_code != 200:
                    print(f"❌ smoke: /api/status 异常 {status.status_code}", file=sys.stderr)
                    ok = False
                else:
                    print("✅ smoke: /api/status 正常")
        finally:
            server.cancel()
            await asyncio.gather(server, return_exceptions=True)
            await hub.stop()

    print("✅ smoke: 启动与关闭流程都正常" if ok else "❌ smoke: 有问题（见上）")
    return 0 if ok else 1


async def cmd_preflight(settings: Settings, *, notify: bool = False, json_only: bool = False) -> int:
    """连通性预检：真的连一次 TG / TMDB / Emby，确认"第一次推送"能不能成。

    默认不发推送（只调 getMe 之类的只读接口）；加 --notify 才会真的发一条测试消息。
    """
    from .selfcheck import run_preflight

    async with Hub(settings) as hub:
        report = await run_preflight(hub, notify=notify)
        if json_only:
            print(report.render_json())
        else:
            print(report.render_text())
        return 0 if report.passed else 1


async def cmd_selfcheck(settings: Settings, *, json_only: bool = False) -> int:
    """部署自检：目录可写性、订阅表、模板、服务配置——全部离线，不发网络请求。"""
    from .selfcheck import run_selfcheck

    report = run_selfcheck(settings)
    if json_only:
        print(report.render_json())
    else:
        print(report.render_text())
    if not report.passed:
        return 1
    return 0 if not report.warnings else 0


async def cmd_feeds(settings: Settings, *, json_only: bool = False, export: str | None = None) -> int:
    """体检所有 RSS 源：能不能抓、抓到多少、最新几条长什么样。

    配 RSS 时最该先跑这个——不需要 Emby/TMDB 也能用。
    """
    import sys as _sys
    from pathlib import Path as _Path

    def info(message: str) -> None:
        print(message, file=_sys.stderr if json_only else _sys.stdout)

    async with Hub(settings) as hub:
        if not settings.subscriptions:
            print("❌ 还没有任何订阅。先编辑 config/subscriptions.yaml（或用网页 UI 添加）")
            return 1

        report = await check_feeds(settings, hub.http)
        if json_only:
            print(report.render_json())
        else:
            print(report.render_text())

        if export:
            path = _Path(export)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(report.render_json(), encoding="utf-8")
            info(f"完整报告（JSON）已写入：{path}")

        # 有源失败就用非零退出码，方便脚本判断
        return 0 if report.healthy else 2


async def cmd_gaps(
    settings: Settings,
    *,
    limit: int | None = None,
    json_only: bool = False,
    export: str | None = None,
    top: int = 20,
    refresh: bool = False,
    scan_only: bool = False,
) -> int:
    """查漏：先扫媒体库找出缺集，再看这些缺集当前能不能在 RSS 里找到。

    scan_only=True 时直接用上次的扫描结果（不重新扫库），速度最快。
    """
    import json as _json
    import sys as _sys
    from pathlib import Path as _Path

    def info(message: str) -> None:
        print(message, file=_sys.stderr if json_only else _sys.stdout)

    async with Hub(settings) as hub:
        if not settings.library.enabled or not settings.tmdb.enabled:
            print("❌ 查漏需要同时配置 Emby/Jellyfin 与 TMDB API Key")
            return 1
        if not settings.subscriptions:
            print("❌ 还没有任何订阅（RSS 源），没有可比的资源")
            return 1

        scan = None
        if scan_only and hub.web is not None and getattr(hub.web, "last_scan", None):
            scan = hub.web.last_scan
            info("使用上次的媒体库扫描结果（未重新扫库）")
        if scan is None:
            if refresh:
                purged = await hub.db.purge_tmdb_cache(settings.scan_cache_ttl)
                info(f"已清理过期 TMDB 缓存 {purged} 条")
            info("第 1 步：扫描媒体库找出缺集…")
            scan = await hub.scanner.scan(limit=limit or settings.scan_max_series)

        info(f"第 2 步：抓取 RSS 并比对（{len(settings.subscriptions)} 条订阅）…")
        report: GapReport = await hub.gapfinder.run(scan)

        if json_only:
            print(report.render_json())
        else:
            print()
            print(report.render_text(top=top))

        if export:
            path = _Path(export)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(report.render_json(), encoding="utf-8")
            info(f"完整报告（JSON）已写入：{path}")

        return 0


async def cmd_test_notify(settings: Settings) -> int:
    async with Hub(settings) as hub:
        if not settings.telegram.enabled:
            print("Telegram 未配置，请先填 .env")
            return 1
        try:
            me = await hub.tg.get_me()
            print(f"机器人：@{me.get('username')} ({me.get('first_name')})")
        except Exception as exc:  # noqa: BLE001
            print(f"❌ 无法连接 Telegram：{exc}")
            return 1
        text = (
            "🔔 <b>rss-media-hub 测试消息</b>\n"
            f"版本 v{__version__}\n"
            f"订阅数量：{len(settings.subscriptions)}\n"
            f"媒体服务器：{settings.library.url or '未配置'}\n"
            f"🕒 {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )
        sent = await hub.tg.send_message(text)
        if sent.ok:
            print("✅ 已发送测试消息，请查看 Telegram")
            return 0
        print(f"❌ 发送失败：{sent.error}")
        return 1


async def cmd_list(settings: Settings) -> int:
    async with Hub(settings) as hub:
        states = {s.id: s for s in await hub.db.all_subs()}
        if not settings.subscriptions:
            print("没有任何订阅")
            return 0
        for sub in settings.subscriptions:
            st = states.get(sub.id)
            if st is None:
                print(f"⚪️ {sub.name}（{sub.id}）— 尚未巡检")
                continue
            icon = {"active": "🟡", "done": "✅", "removed": "🗑", "error": "❌"}.get(st.state, "⚪️")
            counts = await hub.db.count_items(sub.id)
            print(
                f"{icon} {sub.name}（{sub.id}） 入库 {st.owned}/{st.aired or st.total}"
                + (f"｜缺 {st.missing}" if st.missing else "")
                + f"｜登记条目 {counts[0]}（已推送 {counts[1]}）"
                + (f"｜错误 {st.last_error}" if st.last_error else "")
            )
        return 0


async def cmd_add(
    settings: Settings,
    *,
    name: str,
    rss: str = "",
    tmdb_id: int | None = None,
    year: int | None = None,
    season: int | None = None,
) -> int:
    from .config import Subscription as Sub, slugify

    subs = list(settings.subscriptions)
    sub_id = slugify(name)
    if any(s.id == sub_id for s in subs):
        print(f"订阅已存在：{sub_id}")
        return 1
    sub = Sub(id=sub_id, name=name, rss=rss, tmdb_id=tmdb_id, year=year, season=season)
    subs.append(sub)
    save_subscriptions(settings.subs_file, subs)
    print(f"✅ 已写入 {settings.subs_file}：{name}（id={sub_id}）")
    # 顺带发一条「已添加订阅」确认（带海报）。任何失败都不影响订阅本身。
    hub = Hub(settings)
    try:
        await hub.announce_subscription(sub)
    finally:
        await hub.aclose()
    return 0


async def cmd_rm(settings: Settings, key: str, keep_items: bool = False) -> int:
    subs = list(settings.subscriptions)
    target = next((s for s in subs if s.id == key or s.name == key), None)
    if target is None:
        print(f"找不到订阅：{key}")
        return 1
    subs = [s for s in subs if s.id != target.id]
    save_subscriptions(settings.subs_file, subs)
    if not keep_items:
        db = Database(settings.db_file)
        removed = await db.drop_items(target.id)
        await db.delete_sub(target.id)
        db.close()
        print(f"🗑 已删除订阅 {target.name}（同时清理 {removed} 条记录）")
    else:
        print(f"🗑 已删除订阅 {target.name}（保留历史记录）")
    return 0
