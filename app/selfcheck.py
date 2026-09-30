"""部署自检：把"我在这台机器上验证不了、只有你那边能验证"的东西变成一条命令。

动机很实际：这个项目的大部分逻辑我都能用单元测试覆盖，但有四类东西
只有在你的 NAS + 容器里才能确认：

  1. 容器里的挂载目录能不能写（权限、PUID/PGID）
  2. config 目录是否真的可写（"追完自动退订"要回写 subscriptions.yaml）
  3. 订阅表能不能被解析（你手改 YAML 很容易写错缩进）
  4. 镜像里的模板 / 入口脚本有没有问题（宿主机上跑不了 shell）

`selfcheck` 会把这些逐项检查并给出**下一步该怎么办**，而不是丢一堆异常。
刻意不依赖网络，所以离线也能跑。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from .config import ConfigError, Settings, load_settings, load_subscriptions

OK = "ok"
WARN = "warn"
FAIL = "fail"

_ICON = {OK: "✅", WARN: "⚠️ ", FAIL: "❌"}


@dataclass
class CheckItem:
    name: str
    status: str = OK
    detail: str = ""
    fix: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail, "fix": self.fix}


@dataclass
class SelfCheckReport:
    items: list[CheckItem] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0

    @property
    def elapsed(self) -> float:
        return max(0.0, (self.finished_at or time.time()) - self.started_at)

    @property
    def failures(self) -> list[CheckItem]:
        return [i for i in self.items if i.status == FAIL]

    @property
    def warnings(self) -> list[CheckItem]:
        return [i for i in self.items if i.status == WARN]

    @property
    def passed(self) -> bool:
        return not self.failures

    def add(self, name: str, status: str, detail: str = "", fix: str = "") -> CheckItem:
        item = CheckItem(name=name, status=status, detail=detail, fix=fix)
        self.items.append(item)
        return item

    def render_text(self) -> str:
        lines = ["=" * 78, f"部署自检报告（rss-media-hub v{__version__}）", "=" * 78, ""]
        width = max((len(i.name) for i in self.items), default=10)
        for item in self.items:
            lines.append(f" {_ICON.get(item.status, '?')} {item.name:<{width}}  {item.detail}")
            if item.fix and item.status != OK:
                lines.append(f"    {' ' * width}  ↳ {item.fix}")

        lines.append("")
        if self.failures:
            names = "、".join(i.name for i in self.failures[:3])
            lines.append(f"结论：有 {len(self.failures)} 项必须处理（{names}），否则对应功能用不了。")
        elif self.warnings:
            lines.append(f"结论：可以跑，但有 {len(self.warnings)} 项建议处理。")
        else:
            lines.append("结论：全部通过，可以正常运行。")
        lines.append(f"（耗时 {self.elapsed:.2f}s）")
        return "\n".join(lines) + "\n"

    def render_json(self, *, indent: int = 2) -> str:
        return json.dumps(
            {
                "version": __version__,
                "ok": self.passed,
                "failures": len(self.failures),
                "warnings": len(self.warnings),
                "elapsed_seconds": round(self.elapsed, 2),
                "checks": [i.to_dict() for i in self.items],
            },
            ensure_ascii=False,
            indent=indent,
        )


# --------------------------------------------------------------------------
# 各项检查
# --------------------------------------------------------------------------


def check_dir_writable(report: SelfCheckReport, path: Path, label: str, fix_hint: str) -> None:
    """检查目录存在且可写（真正写一个文件再删掉，别只看权限位）。"""
    try:
        if not path.exists():
            report.add(
                f"{label} 目录",
                FAIL,
                f"不存在：{path}",
                f"{fix_hint}（程序会尝试创建；创建失败说明挂载或权限有问题）",
            )
            return
        probe = path / f".selfcheck-{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        report.add(f"{label} 目录", OK, f"可写：{path}")
    except PermissionError as exc:
        uid = os.getuid() if hasattr(os, "getuid") else "?"
        report.add(
            f"{label} 目录",
            FAIL,
            f"不可写：{path}（{exc}）",
            f"当前容器内 uid={uid}。在 NAS 上执行 ls -ln {path.parent} 看属主数字，"
            "把 compose 里的 RMH_UID / RMH_GID 改成那两个数字后重新部署",
        )
    except OSError as exc:
        report.add(f"{label} 目录", FAIL, f"不可写：{path}（{exc}）", fix_hint)


def check_config_files(report: SelfCheckReport, settings: Settings) -> None:
    """配置文件是否就位、能不能解析。"""
    subs_file = settings.subs_file
    if not subs_file.exists():
        report.add(
            "订阅表",
            WARN,
            f"不存在：{subs_file}",
            "首次启动会自动生成模板；如果你删掉了，重启容器即可重新生成",
        )
        return
    try:
        subs = load_subscriptions(subs_file)
    except ConfigError as exc:
        report.add(
            "订阅表",
            FAIL,
            f"解析失败：{exc}",
            "YAML 缩进写错了。注意用空格不要用 Tab；中文字符串建议加引号。"
            "可用网页 UI 的「订阅」页面重建这条订阅",
        )
        return
    except Exception as exc:  # noqa: BLE001
        # 注意：PyYAML 的 ParserError / ScannerError 继承自 MarkedYAMLError，
        # 而不是 ValueError，所以必须单独兜住，否则自检会直接崩掉。
        hint = "检查 YAML 缩进（同一级要对齐、列表项用 - 开头）与文件编码（UTF-8）"
        report.add("订阅表", FAIL, f"解析失败：{type(exc).__name__}: {exc}", hint)
        return

    if not subs:
        report.add(
            "订阅表",
            WARN,
            "还没有任何订阅",
            "去网页 UI 的「订阅」页面添加一条，或编辑 config/subscriptions.yaml",
        )
        return

    feed_count = sum(1 for s in subs if s.is_feed)
    show_count = sum(1 for s in subs if s.is_show)
    disabled = sum(1 for s in subs if not s.enabled)
    report.add(
        "订阅表",
        OK,
        f"{len(subs)} 条订阅（全量 {feed_count} · 按剧追踪 {show_count}"
        + (f" · 停用 {disabled}" if disabled else "")
        + "）",
    )

    # 逐条检查容易配错的地方
    no_rss = [s for s in subs if s.enabled and not s.rss_urls]
    if no_rss:
        report.add(
            "订阅的 RSS 地址",
            FAIL,
            f"{len(no_rss)} 条启用的订阅没填 rss：" + "、".join(s.name for s in no_rss[:3]),
            "在网页 UI 的「订阅」页面补上，或临时把这几条设成 enabled: false",
        )
    else:
        report.add("订阅的 RSS 地址", OK, "所有启用的订阅都有 RSS 地址")

    show_no_tmdb = [s for s in subs if s.enabled and s.is_show and not s.tmdb_id]
    if show_no_tmdb:
        report.add(
            "按剧追踪的 TMDB ID",
            WARN,
            f"{len(show_no_tmdb)} 条 show 订阅没有 tmdb_id，只能靠剧名匹配："
            + "、".join(s.name for s in show_no_tmdb[:3]),
            "同名剧较多时容易认错，建议在 TMDB 上找到剧集页 URL 里的数字填进 tmdb_id",
        )
    else:
        report.add("按剧追踪的 TMDB ID", OK, "没有缺 tmdb_id 的 show 订阅")


def check_templates(report: SelfCheckReport) -> None:
    """镜像里的订阅模板 + 入口脚本结构。

    这些东西在宿主机上跑不了 shell，所以改用 Python 做结构检查。
    """
    root = Path(__file__).resolve().parent.parent
    entry = root / "docker" / "entrypoint.sh"
    if not entry.exists():
        report.add(
            "入口脚本",
            WARN,
            f"没找到 {entry}（源码运行时会这样，容器里一定有）",
            "容器镜像里必然存在；只有从源码直接跑 CLI 才会缺这个文件",
        )
        return

    try:
        text = entry.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        report.add("入口脚本", FAIL, f"读取失败：{exc}", "检查文件是否损坏")
        return

    problems: list[str] = []
    if "set -e" not in text:
        problems.append("缺少 set -e（出错不会中断，可能静默失败）")
    opens = len(re.findall(r"<<'YAML'", text))
    closes = len(re.findall(r"(?m)^YAML\s*$", text))
    if opens != closes:
        problems.append(f"heredoc 不配对（开始 {opens} 个，结束 {closes} 个）")
    if "exec gosu" not in text:
        problems.append("没有降权执行（会以 root 跑，写出的文件属主不对）")

    if problems:
        report.add("入口脚本", FAIL, "；".join(problems), "这是镜像构建问题，请反馈")
    else:
        report.add("入口脚本", OK, "结构正常（set -e、heredoc 配对、会降权执行）")

    # 入口脚本里的订阅模板必须能被解析（它会在首次启动时直接落盘）
    match = re.search(r"<<'YAML'\n(.*?)\nYAML", text, re.S)
    if not match:
        report.add("首次启动模板", WARN, "入口脚本里没找到订阅模板", "首次启动需要你手工创建 subscriptions.yaml")
        return
    try:
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as fh:
            fh.write(match.group(1))
            tmp = Path(fh.name)
        try:
            subs = load_subscriptions(tmp)
        finally:
            tmp.unlink(missing_ok=True)
        report.add("首次启动模板", OK, f"可解析，包含 {len(subs)} 条示例订阅")
    except Exception as exc:  # noqa: BLE001
        report.add("首次启动模板", FAIL, f"解析失败：{exc}", "这是镜像内容问题，请反馈")


def check_services(report: SelfCheckReport, settings: Settings) -> None:
    """三个外部服务的配置是否齐全（只查配置，不发请求——自检要能离线跑）。"""
    if settings.telegram.enabled:
        extra = []
        if settings.telegram.proxy:
            extra.append("已配正向代理")
        if settings.telegram.api_base and "api.telegram.org" not in settings.telegram.api_base:
            extra.append("已配反向代理")
        report.add("Telegram 配置", OK, "Token 与 chat id 都已填" + ("（" + "、".join(extra) + "）" if extra else ""))
    else:
        missing = []
        if not settings.telegram.bot_token:
            missing.append("RMH_TG_BOT_TOKEN")
        if not settings.telegram.chat_id:
            missing.append("RMH_TG_CHAT_ID")
        report.add(
            "Telegram 配置",
            FAIL,
            "缺少：" + "、".join(missing),
            "找 @BotFather 拿 Token；chat id 找 @userinfobot。填完在网页 UI「设置」里保存即生效",
        )

    if settings.library.enabled:
        report.add("Emby/Jellyfin 配置", OK, f"{settings.library.url}")
    else:
        report.add(
            "Emby/Jellyfin 配置",
            WARN,
            "未配置（只影响「按剧追踪」与「查漏」，feed 全量模式照常工作）",
            "填 RMH_EMBY_URL（用 NAS 局域网 IP，别用 localhost）与 RMH_EMBY_API_KEY",
        )

    if settings.tmdb.enabled:
        report.add("TMDB 配置", OK, f"语言 {settings.tmdb.language}")
    else:
        report.add(
            "TMDB 配置",
            WARN,
            "未配置（只影响「按剧追踪」与「查漏」，feed 全量模式照常工作）",
            "去 themoviedb.org 申请 API Key (v3 auth)，填 RMH_TMDB_API_KEY",
        )


def check_ui(report: SelfCheckReport, settings: Settings) -> None:
    if not settings.ui_enabled:
        report.add("网页 UI", OK, "已关闭（RMH_UI_ENABLED=false），仅保留 /healthz")
        return
    if settings.ui_token:
        report.add("网页 UI", OK, f"已启用，端口 {settings.health_port}，需要口令访问")
    else:
        report.add(
            "网页 UI",
            WARN,
            f"已启用但没设访问口令（端口 {settings.health_port}）",
            "同一局域网内任何人都能打开界面并改你的配置。到网页 UI「设置」里设置访问口令（RMH_UI_TOKEN）",
        )


def check_storage(report: SelfCheckReport, settings: Settings) -> None:
    """数据卷的持久化状态：能不能写、有没有历史数据。"""
    db = settings.db_file
    if db.exists():
        size_kb = db.stat().st_size / 1024
        has_wal = db.with_name(db.name + "-wal").exists()
        report.add(
            "状态数据库",
            OK,
            f"{db}（{size_kb:.0f} KB{('，含 WAL' if has_wal else '')}）"
            + ("— 已有历史记录，重启不会重复推送" if size_kb > 20 else ""),
        )
    else:
        report.add(
            "状态数据库",
            WARN,
            f"还没有 {db}（首次运行会自动创建）",
            "这个文件记录「哪些条目已经推过」，删掉会导致历史条目被重新推送一遍；建议保持挂载",
        )

    # 磁盘剩余空间（写不进去时报错很难懂，先提醒）
    try:
        usage = shutil.disk_usage(str(settings.state_dir if settings.state_dir.exists() else Path("/")))
        free_gb = usage.free / (1024**3)
        if free_gb < 0.2:
            report.add("磁盘空间", FAIL, f"仅剩 {free_gb:.2f} GB", "清理磁盘，否则 SQLite 写入会失败")
        else:
            report.add("磁盘空间", OK, f"剩余 {free_gb:.1f} GB")
    except Exception:  # noqa: BLE001
        pass


def run_selfcheck(settings: Settings) -> SelfCheckReport:
    """跑完整自检（不发任何网络请求）。"""
    report = SelfCheckReport()
    report.add("版本", OK, f"rss-media-hub v{__version__}（Python {sys.version.split()[0]}）")
    report.add("运行身份", OK, f"uid={os.getuid() if hasattr(os, 'getuid') else '?'} 工作目录={Path.cwd()}")

    check_dir_writable(
        report, settings.config_dir, "配置", "确认 compose 里挂载了 ./config:/config 且宿主机目录存在"
    )
    check_dir_writable(
        report, settings.state_dir, "状态", "确认 compose 里挂载了 ./state:/state 且宿主机目录存在"
    )
    check_config_files(report, settings)
    check_storage(report, settings)
    check_templates(report)
    check_services(report, settings)
    check_ui(report, settings)

    report.finished_at = time.time()
    return report


# --------------------------------------------------------------------------
# 连通性预检（会发网络请求，和上面的离线自检分开）
# --------------------------------------------------------------------------


async def _probe_tmdb_series(client: Any, tmdb_id: int, fast_kwargs: dict[str, Any]) -> Any:
    """用"快速失败"参数查一次 TMDB，返回结果。

    真实 TmdbClient 支持 fast() 上下文；测试用的假客户端没有，所以这里
    也要能优雅降级——不能用 `with client.fast()` 硬套（那会 AttributeError）。
    """
    fast = getattr(client, "fast", None)
    if callable(fast):
        with fast(**fast_kwargs):
            return await client.series(None, tmdb_id=tmdb_id)
    return await client.series(None, tmdb_id=tmdb_id)


async def run_preflight(hub: Any, *, notify: bool = False) -> SelfCheckReport:
    """真正连一次三个外部服务，确认"第一次推送"能不能成。

    和 run_selfcheck 的区别：那个只看配置填没填，这个**真的发请求**。
    所以它默认不发送任何推送（只调 getMe / 查 TMDB / 查 Emby），
    加 notify=True 才会真的发一条测试消息。
    """
    from .emby import EmbyError
    from .tmdb import TmdbError

    settings: Settings = hub.settings
    report = SelfCheckReport()
    report.add(
        "预检模式",
        OK,
        "会真实访问外部服务" + ("（并发送一条测试消息）" if notify else "（不会发送推送）")
        + "；为快速出结果，重试次数和超时都比轮询时更小",
    )

    # 预检是给人当场看的，所以用更短的重试与超时（轮询时才需要更多重试）
    fast_kwargs = {"retries": 1, "timeout": 8.0}

    # ---------- Telegram ----------
    if not settings.telegram.enabled:
        report.add(
            "Telegram 连通",
            FAIL,
            "未配置（缺 Token 或 chat id），无法推送",
            "填 RMH_TG_BOT_TOKEN 与 RMH_TG_CHAT_ID；国内还需 RMH_TG_PROXY 或 RMH_TG_API_BASE",
        )
    else:
        try:
            me = await hub.tg.get_me()
            report.add("Telegram 连通", OK, f"机器人 @{me.get('username')}（{me.get('first_name')}）")
        except Exception as exc:  # noqa: BLE001
            hint = "国内直连 api.telegram.org 通常不通：在设置里填正向代理 RMH_TG_PROXY（如 http://192.168.31.142:10809）"
            report.add("Telegram 连通", FAIL, f"连接失败：{exc}", hint)
        else:
            if notify:
                try:
                    sent = await hub.tg.send_message(
                        "✅ <b>rss-media-hub 预检消息</b>\n"
                        f"版本 v{__version__}\n"
                        f"订阅 {len(settings.subscriptions)} 条\n"
                        "如果你看到这条，说明推送链路是通的。"
                    )
                    if sent.ok:
                        report.add("Telegram 推送", OK, f"测试消息已送达（message_id={sent.message_id}）")
                    else:
                        report.add(
                            "Telegram 推送",
                            FAIL,
                            f"发送失败：{sent.error}",
                            "常见原因：chat id 填错、机器人没被你 /start 过、或群里没给它发言权限",
                        )
                except Exception as exc:  # noqa: BLE001
                    report.add("Telegram 推送", FAIL, f"发送异常：{exc}", "检查 chat id 与机器人权限")

    # ---------- TMDB ----------
    probe_tmdb_id = None
    for sub in settings.subscriptions:
        if sub.tmdb_id:
            probe_tmdb_id = sub.tmdb_id
            break
    if not settings.tmdb.enabled:
        report.add(
            "TMDB 连通",
            WARN,
            "未配置（只影响「按剧追踪」与「查漏」，feed 全量模式照常工作）",
            "填 RMH_TMDB_API_KEY；国内还需 RMH_TMDB_PROXY 或 RMH_TMDB_API_BASE",
        )
    else:
        try:
            if probe_tmdb_id:
                # 只查一次：fast 模式在调用内部生效（外面再查一次会用回默认重试/超时）
                series = await _probe_tmdb_series(hub.tmdb, probe_tmdb_id, fast_kwargs)
                report.add(
                    "TMDB 连通",
                    OK,
                    f"查询成功：《{series.name}》{series.total_seasons} 季 / {series.total_episodes} 集",
                )
            else:
                raw = await hub.tmdb._get("/configuration", **fast_kwargs)
                ok = bool(raw.get("images") or raw.get("change_keys"))
                report.add("TMDB 连通", OK if ok else WARN, "接口可达" + ("" if ok else "，但返回内容异常"))
        except TmdbError as exc:
            report.add(
                "TMDB 连通",
                FAIL,
                f"查询失败：{exc}",
                "国内直连 api.themoviedb.org 通常不通：填 RMH_TMDB_PROXY（正向代理）或 RMH_TMDB_API_BASE（自建反代）",
            )
        except Exception as exc:  # noqa: BLE001
            report.add("TMDB 连通", FAIL, f"异常：{exc}", "检查网络与 RMH_TMDB_* 配置")

    # ---------- Emby / Jellyfin ----------
    if not settings.library.enabled:
        report.add(
            "媒体服务器连通",
            WARN,
            "未配置（只影响「按剧追踪」与「查漏」，feed 全量模式照常工作）",
            "填 RMH_EMBY_URL（用 NAS 局域网 IP）与 RMH_EMBY_API_KEY",
        )
    else:
        try:
            lib_fast = getattr(hub.library, "fast", None)
            if callable(lib_fast):
                with lib_fast(timeout=5.0):
                    info = await hub.library.ping()
                    kind = await hub.library.detect_kind()
            else:
                info = await hub.library.ping()
                kind = await hub.library.detect_kind()
            report.add(
                "媒体服务器连通",
                OK,
                f"{kind} {info.get('Version') or '?'} @ {settings.library.url}",
            )
        except EmbyError as exc:
            report.add(
                "媒体服务器连通",
                FAIL,
                f"连接失败：{exc}",
                "① 地址要用 NAS 局域网 IP，不能用 localhost（容器里的 localhost 是容器自己）"
                " ② 确认 API 密钥有效 ③ https 自签证书需要 RMH_EMBY_VERIFY_TLS=false",
            )
        except Exception as exc:  # noqa: BLE001
            report.add("媒体服务器连通", FAIL, f"异常：{exc}", "检查地址、密钥与网络")
        else:
            try:
                items = await hub.library.list_series(limit_total=5)
                if items:
                    sample = items[0]
                    report.add(
                        "媒体库可读",
                        OK,
                        f"读到 {len(items)} 部剧（示例：{sample.get('Name')}）"
                        + ("，已刮削 TMDB ID" if (sample.get("ProviderIds") or {}).get("Tmdb") else "，但示例没刮削 TMDB ID"),
                    )
                else:
                    report.add(
                        "媒体库可读",
                        WARN,
                        "接口通了，但一部剧都没读到",
                        "确认媒体库里有剧集，且该 API 密钥对应的用户可以访问这些库",
                    )
            except Exception as exc:  # noqa: BLE001
                report.add("媒体库可读", FAIL, f"读取剧集列表失败：{exc}", "检查密钥权限")

    # ---------- 订阅源（不重复抓，只确认有没有配） ----------
    if settings.subscriptions:
        enabled = [s for s in settings.subscriptions if s.enabled]
        no_rss = [s for s in enabled if not s.rss_urls]
        if no_rss:
            report.add(
                "订阅源",
                FAIL,
                f"{len(no_rss)} 条启用的订阅没填 RSS：" + "、".join(s.name for s in no_rss[:3]),
                "在网页 UI 的「订阅」页面补上",
            )
        else:
            report.add(
                "订阅源",
                OK,
                f"{len(enabled)} 条启用的订阅都有 RSS；用 `feeds` 命令可逐个验证能否抓取",
            )
    else:
        report.add("订阅源", WARN, "还没有任何订阅", "去网页 UI 的「订阅」页面添加一条")

    report.finished_at = time.time()
    return report
