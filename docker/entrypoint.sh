#!/bin/sh
# ============================================================================
#  容器入口脚本
#
#  为什么需要它：宿主机 bind mount 进来的 ./config 目录属主通常是宿主用户，
#  而容器内以 uid 1000 运行。这里先用 root 建目录 + 修属主，再降权执行程序，
#  避免"首次启动就因权限不足写不了 subscriptions.yaml"。
# ============================================================================
set -e

CONFIG_DIR="${RMH_CONFIG_DIR:-/config}"
STATE_DIR="${RMH_STATE_DIR:-/state}"
APP_UID="${RMH_UID:-1000}"
APP_GID="${RMH_GID:-1000}"

ensure_dir() {
    dir="$1"
    if [ ! -d "$dir" ]; then
        mkdir -p "$dir" 2>/dev/null || true
    fi
    if [ "$(id -u)" = "0" ]; then
        chown -R "$APP_UID:$APP_GID" "$dir" 2>/dev/null || true
    fi
}

ensure_dir "$CONFIG_DIR"
ensure_dir "$STATE_DIR"

# 首次启动没有配置文件时，先落一份带注释的模板，用户直接改就行
if [ ! -f "$CONFIG_DIR/subscriptions.yaml" ]; then
    cat > "$CONFIG_DIR/subscriptions.yaml" <<'YAML'
# ============================================================================
#  订阅表
#  改完执行：docker compose restart（运行中每 30 秒也会自动热重载）
#  也可以完全用网页 UI 管理：http://<NAS IP>:18080/ → 「订阅」标签
#
#  两种模式（mode）：
#    feed = 订阅源全量：RSS 里有什么推什么，不比对媒体库，不需要 tmdb_id
#    show = 按剧追踪：只推这部剧，算入库进度，追完自动退订
#    留空时：填了 tmdb_id 就按 show，否则按 feed
#
#  字段速查：
#    id                唯一标识，可省略（自动由 name 生成）
#    name              名称（show 填剧名；feed 随便填个便于识别的）
#    mode              feed | show
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
  #  最常用形态：给一个 RSS，来新数据就推给你
  #  刷新间隔看 runtime.poll_interval（默认 180 秒 = 3 分钟）
  # ==========================================================================
  - id: my-pt
    name: 我的 PT 站
    mode: feed
    # 【必改】换成你自己的 RSS 地址（passkey 就在地址里）
    rss: https://your-pt-site.example/rss?passkey=YOUR_PASSKEY
    # 首轮就把 RSS 里现有条目推给你；不想被历史条目刷屏就改成 true
    seed: false
    # 想只看 2160p、或排除预告花絮，就打开下面两行
    # quality: ["2160p"]
    # exclude_filter: "预告|花絮|OST"
YAML
    if [ "$(id -u)" = "0" ]; then
        chown "$APP_UID:$APP_GID" "$CONFIG_DIR/subscriptions.yaml" 2>/dev/null || true
    fi
    echo "已生成订阅模板：$CONFIG_DIR/subscriptions.yaml"
fi

# 以 root 启动时降权，避免程序以 root 写文件
if [ "$(id -u)" = "0" ] && [ "${RMH_ALLOW_ROOT:-0}" != "1" ]; then
    exec gosu "$APP_UID:$APP_GID" "$@"
fi

exec "$@"
