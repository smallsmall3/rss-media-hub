"""命令行入口：python -m app.main <命令>"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from . import __version__
from .config import ConfigError, Settings, dump_yaml_file, load_settings, slugify
from .main import (
    cmd_add,
    cmd_check,
    cmd_feeds,
    cmd_gaps,
    cmd_list,
    cmd_preflight,
    cmd_rm,
    cmd_run,
    cmd_scan,
    cmd_selfcheck,
    cmd_test_notify,
    cmd_web,
)

CONFIG_TEMPLATE = """\
# ============================================================================
#  rss-media-hub 配置文件
#  密钥建议只写在 .env 里（环境变量优先于本文件），本文件只放"规则"。
#  改完执行：docker compose restart
# ============================================================================

telegram:
  # 留空则用环境变量 RMH_TG_BOT_TOKEN / RMH_TG_CHAT_ID
  bot_token: ""
  chat_id: ""
  thread_id: ""
  api_base: ""            # 国内服务器可填 TG 反代，例如 https://tgapi.example.com
  send_poster: true       # 推送时附带海报
  disable_notification: false

tmdb:
  api_key: ""
  api_base: ""            # 国内可填反代，例如 https://tmdb.example.com
  language: zh-CN

library:
  # Emby / Jellyfin 地址与 API 密钥（必填）
  url: ""
  api_key: ""
  user_id: ""             # 老版 Emby 需要；留空自动取管理员
  kind: auto              # auto | emby | jellyfin
  count_aired_only: true  # 只统计"已播出"的集数作为分母（避免未播出的集永远追不完）
  include_specials: false # 是否把特别篇(Season 0)计入
  cache_ttl: 300          # 媒体库查询结果缓存秒数，避免频繁打 Emby
  verify_tls: false       # 自签证书环境保持 false

runtime:
  # RSS 刷新间隔（秒）：想每 2-5 分钟看一次新数据就填 120~300
  poll_interval: 180
  # show（按剧追踪）模式的入库巡检间隔，feed 模式用不到
  reconcile_interval: 1800
  seed_silent: true         # 首次运行只登记历史条目、不推送
  health_port: 8080         # /healthz 与 /status
  log_level: INFO           # DEBUG | INFO | WARNING
  scan_cache_ttl: 43200     # 全库扫描时 TMDB 结果缓存时长（秒）
  scan_concurrency: 5       # 全库扫描并发数，调大更容易被 TMDB 限流
  scan_max_series: 0        # 单次最多扫描多少部剧，0=不限

ui:
  enabled: true             # 是否提供网页 UI（/healthz 始终可用）
  token: ""                 # 网页 UI 访问口令，强烈建议设置
"""

SUBS_TEMPLATE = """\
# ============================================================================
#  订阅表
#  改完执行：docker compose restart（运行中也每 30 秒自动热重载）
#  也可以完全用网页 UI 管理：http://<NAS IP>:18080/ → 「订阅」标签
#
#  两种模式（mode）：
#    feed = 订阅源全量：RSS 里出现什么就推什么，不比对媒体库，
#           不需要 tmdb_id，也不需要配 Emby/TMDB。当"RSS 播报器"用。
#    show = 按剧追踪：只推这部剧的条目，每轮比对媒体库算入库进度，
#           全部入库后推送完成通知并自动退订。
#    留空时：填了 tmdb_id 就按 show，否则按 feed。
#
#  字段速查：
#    id                唯一标识，可省略（自动由 name 生成）
#    name              名称（show 填剧名；feed 随便填个便于识别的）
#    mode              feed | show（见上）
#    tmdb_id           show 模式强烈建议填写：TMDB 剧集页 URL 里的数字
#    year              首播年份，同名剧较多时必填
#    season            只关注某一季；省略=全部季
#    rss               PT 站 RSS 地址，可写多条（空格/逗号分隔）
#    name_filter       正则，只推送命中的标题
#    exclude_filter    正则，命中即忽略（例如排除预告、花絮）
#    quality           关键字白名单，例如 ["1080p", "2160p"]
#    tmdb_required     feed 模式下：只有能匹配到 TMDB 的条目才推送
#    seed              首轮是否静默登记历史条目（feed 默认 false=全部补推）
#    notify_new        发现新种是否推送（默认 true）
#    remove_when_done  全部入库后自动删除本订阅（仅 show 模式，默认 true）
# ============================================================================

subscriptions:
  # ==========================================================================
  #  这就是最常用的形态：给一个 RSS，来新数据就推给你
  #  刷新间隔在 runtime.poll_interval（默认 180 秒 = 3 分钟）
  # ==========================================================================
  - id: my-pt
    name: 我的 PT 站
    mode: feed
    # 【必改】把下面这行换成你自己的 RSS 地址（passkey 就在地址里）
    rss: https://your-pt-site.example/rss?passkey=YOUR_PASSKEY
    # 首轮就把 RSS 里现有的条目推给你；不想被历史条目刷屏就改成 true
    seed: false
    # 想只看 2160p、或排除预告花絮，就打开下面这两行
    # quality: ["2160p"]
    # exclude_filter: "预告|花絮|OST"

  # ==========================================================================
  #  可选：按剧追踪（算入库进度、追完自动退订）
  #  想要这个就把上面那条留着，再按需加下面这种；两条订阅互不影响
  # ==========================================================================
  # - id: example-show
  #   name: 某部剧
  #   mode: show
  #   tmdb_id: 12345
  #   rss: https://your-pt-site.example/rss?passkey=YOUR_PASSKEY&cat=2
  #   remove_when_done: true
"""


def ensure_config_files(settings: Settings) -> None:
    """首次运行时生成带注释的配置模板，避免用户面对空目录发呆。"""
    if not settings.config_file.exists():
        settings.config_file.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        print(f"已生成配置模板：{settings.config_file}")
    if not settings.subs_file.exists():
        settings.subs_file.write_text(SUBS_TEMPLATE, encoding="utf-8")
        print(f"已生成订阅模板：{settings.subs_file}（编辑后重启容器生效）")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    # httpx 的 DEBUG 日志会打印带 passkey 的完整 URL，压到 WARNING
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rss-media-hub",
        description="PT RSS 订阅器 + Emby/Jellyfin 入库比对 + Telegram 推送（追完自动退订）",
    )
    parser.add_argument("--config-dir", default=None, help="配置目录（默认取 RMH_CONFIG_DIR 或 ./config）")
    parser.add_argument("--state-dir", default=None, help="状态目录（默认取 RMH_STATE_DIR 或 ./state）")
    parser.add_argument("--version", action="version", version=f"rss-media-hub {__version__}")

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run", help="常驻运行（Docker 默认命令）")
    sub.add_parser("web", help="只跑健康检查接口（调试用）")

    p_check = sub.add_parser("check", help="立刻做一次入库比对并在终端输出")
    p_check.add_argument("target", nargs="?", help="只检查某个订阅 id 或名称")
    p_check.add_argument("--notify", action="store_true", help="同时把结果推到 Telegram")

    sub.add_parser("test-notify", help="发送一条 Telegram 测试消息")
    sub.add_parser("list", help="列出订阅与最近一次巡检结果")

    p_scan = sub.add_parser("scan", help="扫描整个媒体库，列出每部剧的入库集数与缺集")
    p_scan.add_argument("--limit", type=int, default=None, help="最多检查多少部剧（默认取 RMH_SCAN_MAX_SERIES）")
    p_scan.add_argument("--top", type=int, default=15, help="每类最多展示多少条（默认 15）")
    p_scan.add_argument("--json", dest="json_only", action="store_true", help="只输出 JSON，便于脚本处理")
    p_scan.add_argument("--export", default=None, help="把完整 JSON 报告写到指定文件")
    p_scan.add_argument("--show-complete", action="store_true", help="同时列出已完整的剧")
    p_scan.add_argument("--refresh", action="store_true", help="扫描前先清理过期的 TMDB 缓存")

    p_gaps = sub.add_parser("gaps", help="查漏：媒体库缺的集 × 当前 RSS 里现成的资源")
    p_gaps.add_argument("--limit", type=int, default=None, help="扫库时最多检查多少部剧")
    p_gaps.add_argument("--top", type=int, default=20, help="每类最多展示多少条（默认 20）")
    p_gaps.add_argument("--json", dest="json_only", action="store_true", help="只输出 JSON（提示走 stderr）")
    p_gaps.add_argument("--export", default=None, help="把完整 JSON 报告写到指定文件")
    p_gaps.add_argument("--refresh", action="store_true", help="扫库前先清理过期的 TMDB 缓存")
    p_gaps.add_argument("--scan-only", dest="scan_only", action="store_true", help="复用上次扫描结果，不重新扫库")

    p_feeds = sub.add_parser("feeds", help="体检所有 RSS 源：能不能抓、抓到多少、最新几条")
    p_feeds.add_argument("--json", dest="json_only", action="store_true", help="只输出 JSON")
    p_feeds.add_argument("--export", default=None, help="把完整 JSON 报告写到指定文件")

    p_self = sub.add_parser("selfcheck", help="部署自检：目录权限、订阅表、模板、服务配置（离线可用）")
    p_self.add_argument("--json", dest="json_only", action="store_true", help="只输出 JSON")

    p_pre = sub.add_parser("preflight", help="连通性预检：真的连一次 TG / TMDB / Emby（会发网络请求）")
    p_pre.add_argument("--notify", action="store_true", help="同时真的发一条 Telegram 测试消息")
    p_pre.add_argument("--json", dest="json_only", action="store_true", help="只输出 JSON")

    p_add = sub.add_parser("add", help="新增订阅（写入 subscriptions.yaml）")
    p_add.add_argument("name", help="剧名")
    p_add.add_argument("--rss", default="", help="PT 站 RSS 地址")
    p_add.add_argument("--tmdb-id", type=int, default=None, help="TMDB 剧集 id")
    p_add.add_argument("--year", type=int, default=None, help="首播年份")
    p_add.add_argument("--season", type=int, default=None, help="只订阅某一季")

    p_rm = sub.add_parser("rm", help="删除订阅（并清理状态）")
    p_rm.add_argument("target", help="订阅 id 或名称")
    p_rm.add_argument("--keep-items", action="store_true", help="保留历史条目记录")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "run"

    try:
        settings = load_settings(args.config_dir, args.state_dir)
    except ConfigError as exc:
        print(f"❌ 配置错误：{exc}", file=sys.stderr)
        return 1

    if command in {"run", "web"}:
        ensure_config_files(settings)

    setup_logging(settings.log_level)

    if command == "run":
        return asyncio.run(cmd_run(settings))
    if command == "web":
        return asyncio.run(cmd_web(settings))
    if command == "check":
        return asyncio.run(cmd_check(settings, args.target, args.notify))
    if command == "scan":
        return asyncio.run(
            cmd_scan(
                settings,
                limit=args.limit,
                json_only=args.json_only,
                export=args.export,
                show_complete=args.show_complete,
                top=args.top,
                refresh=args.refresh,
            )
        )
    if command == "gaps":
        return asyncio.run(
            cmd_gaps(
                settings,
                limit=args.limit,
                json_only=args.json_only,
                export=args.export,
                top=args.top,
                refresh=args.refresh,
                scan_only=args.scan_only,
            )
        )
    if command == "preflight":
        return asyncio.run(cmd_preflight(settings, notify=args.notify, json_only=args.json_only))
    if command == "selfcheck":
        return asyncio.run(cmd_selfcheck(settings, json_only=args.json_only))
    if command == "feeds":
        return asyncio.run(cmd_feeds(settings, json_only=args.json_only, export=args.export))
    if command == "test-notify":
        return asyncio.run(cmd_test_notify(settings))
    if command == "list":
        return asyncio.run(cmd_list(settings))
    if command == "add":
        return asyncio.run(
            cmd_add(
                settings,
                name=args.name,
                rss=args.rss,
                tmdb_id=args.tmdb_id,
                year=args.year,
                season=args.season,
            )
        )
    if command == "rm":
        return asyncio.run(cmd_rm(settings, args.target, keep_items=args.keep_items))

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
