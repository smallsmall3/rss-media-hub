# RSS Media Hub

> PT 站 RSS 订阅器 → 自动比对你的 Emby/Jellyfin 媒体库 → Telegram 推送入库进度 → **全部入库后自动退订**。

一句话流程：

```
PT 站 RSS ──轮询──► 按订阅规则过滤 ──► Telegram 推「发现新种」
                        │
                        └──► 同一时刻查 Emby/Jellyfin
                              │
                              ├─ TMDB 拿「全部集数」（分母）
                              ├─ 媒体库拿「已入库集数」（分子）
                              ├─ 推送「入库 7/12 集，待入库 S01E08-E12」
                              └─ 分子 = 分母 时 → 推送「订阅完成」+ 自动删除订阅
```

---

## 1. 特性

| 能力 | 说明 |
|---|---|
| PT 站 RSS 订阅 | 直接吃带 passkey 的 RSS 地址，`enclosure` 直链 / magnet / 正文 magnet 都能解析 |
| 多维过滤 | 正则包含、正则排除、画质关键字白名单（1080p/2160p/HDR…） |
| 集号识别 | `S01E02`、`2x07`、`第05集`、`- 07`（动漫绝对集数）、`全12集` 都能认出来 |
| 入库比对 | 通过 Emby / Jellyfin API 取真实已入库集数，不猜文件名 |
| TMDB 分母 | 用 TMDB 的季/集结构当分母，可只统计「已播出」，避免更新中的剧永远追不完 |
| 推送进度 | 每次推新种或新入库都带 `██████░░ 7/12 集（58%）｜待入库 S01E08-E12` |
| 追完退订 | 分子=分母 时推送完成通知，并把订阅从 `subscriptions.yaml` 里删掉（可关） |
| 停机恢复 | 服务挂掉期间你的下载器入库的集，重启后会自动补推入库通知 |
| 不刷屏 | 首次启动只「登记」RSS 里的历史条目，不会一上线就推几百条 |
| 热重载 | 改完 `subscriptions.yaml` 30 秒内自动生效，不用重启容器 |
| 零依赖运维 | 单容器、SQLite 存状态、内置 `/healthz` + `/status` HTTP 接口 |

**它不做什么**：不接管下载器。你只需要在 qBittorrent/Transmission 里给这个 RSS 地址配好自动下载规则，本服务负责「通知 + 记账 + 退订」。

---

## 2. 快速开始

### 2.1 准备三样东西

1. **Telegram 机器人**：找 [@BotFather](https://t.me/BotFather) 发 `/newbot` 拿 Token；再用 [@userinfobot](https://t.me/userinfobot) 拿你的 chat id。
2. **TMDB API Key**：<https://www.themoviedb.org/settings/api> 申请，用 **API Key (v3 auth)** 那串 32 位字符串。
3. **Emby / Jellyfin API 密钥**：
   - Emby：后台 → 高级 → API 密钥 → 新建
   - Jellyfin：控制台 → API 密钥 → 新建

### 2.2 起容器

```bash
git clone <这个项目>  rss-media-hub && cd rss-media-hub
cp .env.example .env
vim .env                # 填 TG / TMDB / Emby 三组密钥
docker compose up -d --build
docker compose logs -f  # 看到 "Telegram 机器人：@xxx" 就成功了
```

首次启动会自动生成 `config/subscriptions.yaml`，把里面的示例换成你自己的订阅：

```yaml
subscriptions:
  - id: some-show
    name: 某部剧
    tmdb_id: 12345                     # TMDB 剧集页 URL 里的数字
    rss: https://pt.example/rss?passkey=你的PASSKEY&cat=2
    quality: ["1080p", "2160p"]
    exclude_filter: "预告|花絮|OST"
    remove_when_done: true
```

```bash
docker compose restart      # 或等 30 秒，程序会自动热重载
```

### 2.3 验证

```bash
docker compose exec rss-media-hub python -m app test-notify   # 发一条 TG 测试消息
docker compose exec rss-media-hub python -m app check         # 立刻做一次入库比对
docker compose exec rss-media-hub python -m app list          # 看所有订阅与进度
curl http://127.0.0.1:18080/status                            # 机器可读的状态
```

`check` 的输出长这样：

```
订阅                        入库/总     已播出    状态 / 缺失
------------------------------------------------------------------------------------
庆余年 第二季               13/36       13        ⏳ 追更中｜缺 S02E14-S02E36
进击的巨人                  87/87       87        ✅ 已完成
无职转生                    0/23        12        🕐 未入库｜缺 S01E01-S01E12
```

---

## 3. 配置参考

密钥优先读环境变量（`.env`），其次读 `config/config.yaml`。**建议密钥只放 `.env`**，`config/` 目录可以安全地备份/共享。

### 3.1 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `RMH_TG_BOT_TOKEN` | ✅ | — | Telegram 机器人 Token |
| `RMH_TG_CHAT_ID` | ✅ | — | 私聊填 user id；群/频道填 `-100xxxxxxxxxx` |
| `RMH_TG_THREAD_ID` | | 空 | 群里的「话题」ID，没有就留空 |
| `RMH_TG_API_BASE` | | 官方 | **反向**代理地址（能替换 `api.telegram.org` 的自建域名） |
| `RMH_TG_PROXY` | | 空 | **正向**代理，国内科学上网端口，如 `http://192.168.31.142:10809`；支持 `socks5://` |
| `RMH_TMDB_API_KEY` | ✅ | — | TMDB v3 api_key |
| `RMH_TMDB_API_BASE` | | 官方 | TMDB **反向**代理，例如 `https://tmdb.example.com` |
| `RMH_TMDB_PROXY` | | 空 | TMDB **正向**代理，通常和 `RMH_TG_PROXY` 填同一个 |
| `RMH_TMDB_LANGUAGE` | | `zh-CN` | 标题语言 |
| `RMH_EMBY_URL` | ✅ | — | 例如 `http://192.168.1.10:8096` |
| `RMH_EMBY_API_KEY` | ✅ | — | Emby/Jellyfin API 密钥 |
| `RMH_EMBY_USER_ID` | | 空 | 老版本 Emby 需要；留空会自动取管理员 |
| `RMH_EMBY_COUNT_AIRED_ONLY` | | `true` | `true`=分母只算已播出集数 |
| `RMH_EMBY_INCLUDE_SPECIALS` | | `false` | 是否把特别篇（Season 0）算进去 |
| `RMH_EMBY_VERIFY_TLS` | | `false` | 自签证书保持 false |
| `RMH_POLL_INTERVAL` | | `900` | RSS 轮询间隔（秒） |
| `RMH_RECONCILE_INTERVAL` | | `1800` | 入库巡检间隔（秒） |
| `RMH_SEED_SILENT` | | `true` | 首次运行静默登记历史条目 |
| `RMH_HEALTH_PORT` | | `8080` | `/healthz`、`/status` 端口，填 0 关闭 |
| `RMH_LOG_LEVEL` | | `INFO` | `DEBUG` / `INFO` / `WARNING` |
| `RMH_SCAN_CACHE_TTL` | | `43200` | 全库扫描时 TMDB 结果缓存时长（秒，默认 12 小时） |
| `RMH_SCAN_CONCURRENCY` | | `5` | 扫描并发数，调大更容易被 TMDB 限流 |
| `RMH_SCAN_MAX_SERIES` | | `0` | 单次最多扫描多少部剧，`0`=不限（库很大时建议先设 50 试水） |
| `RMH_UI_TOKEN` | | 空 | 网页 UI 访问口令，**强烈建议设置**（不设则同局域网任何人都能改你的配置） |
| `RMH_UI_ENABLED` | | `true` | 设 `false` 可关掉网页 UI，只保留 `/healthz` |

### 3.2 订阅字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | | 唯一标识，留空自动由 `name` 生成（中文名会生成 `sub-xxxxxxxx` 短哈希） |
| `name` | ✅ | 剧名，用于匹配 TMDB 与媒体库 |
| `tmdb_id` | ⭐ | **强烈建议填**，避免同名剧匹配错。填了就不需要 `year` |
| `year` | | 首播年份，同名剧多时有用 |
| `season` | | 只关注某一季；留空=全部季 |
| `rss` | ✅ | PT 站 RSS 地址，可写多个（空格或逗号分隔） |
| `name_filter` | | 正则，**只推送**命中的标题 |
| `exclude_filter` | | 正则，命中即**忽略**（如 `预告\|花絮\|OST`） |
| `quality` | | 关键字白名单列表，如 `["1080p","2160p"]`；命中任一即可 |
| `notify_new` | | 发现新种是否推送，默认 `true` |
| `remove_when_done` | | 全部入库后自动删除本订阅，默认 `true` |

> **怎么拿 `tmdb_id`**：打开 TMDB 剧集页，URL 形如
> `https://www.themoviedb.org/tv/`**`1396`**`-breaking-bad`，中间那个数字就是。

### 3.3 两种工作模式（先看这个，决定你怎么配）

| | `mode: feed`（订阅源全量） | `mode: show`（按剧追踪） |
|---|---|---|
| 干什么 | 给一个 RSS，**出现新数据就推给你** | 只追一部剧，算入库进度，追完自动退订 |
| 需要 tmdb_id | 不需要 | 需要（或至少给剧名） |
| 需要 Emby/TMDB | **不需要** | 需要 |
| 推送内容 | 每条新种：标题/集号/体积/时间/下载链接 | 新种 + 「入库 7/12 集，待入库 S01E08-E12」 |
| 明显不同 | 不做去重之外的任何判断，就是转发 | 会比对媒体库、判断追完、自动删订阅 |

**feed 模式的最小配置**（你描述的场景就是这个）：

```yaml
subscriptions:
  - id: my-pt
    name: 我的 PT 站
    mode: feed
    rss: https://pt.example/rss?passkey=你的PASSKEY
    seed: false          # 首轮就把现有条目推过来（想先静默就写 true）
```

再把刷新间隔设成 2-5 分钟（`RMH_POLL_INTERVAL=180` 即 3 分钟）：

```
每 3 分钟抓一次 RSS ──► 发现没见过的条目 ──► 立刻推 Telegram
                       └─ 已经推过的不再重复
```

工作流程是这样的：

1. **第一次轮询**：把 RSS 里现有条目全部推给你（`seed: false`）；若写 `seed: true` 则只登记不推送
2. **之后每 3 分钟**：只推"上次没见过"的条目，靠 `guid`/下载链接做去重，容器重启也不会重复
3. **不再需要的条目**：用 `quality`、`exclude_filter` 过滤掉（比如只要 2160p、排除预告花絮）

写 `TMDB` 的 `tmdb_id` 会让模式变成 `show`。想两个都要就写两条订阅。

### 3.4 推送长什么样

通知不是把 RSS 标题原样转发——那样又长又乱。程序会**整理成信息卡**，
并且把**同一集的不同版本合并**（PT 站一集常同时发 2160p/1080p/720p，逐个推会把人烦死）：

```
📡 我的 PT 站
🆕 2 集 / 4 个版本 · RSS 全量

📺 S01E05 · 12.50 GB
   2160p · HDR · HEVC · WEB-DL · DDP
   🔗 下载
   ↳ 还有 1080p AVC 3.20 GB ｜ 720p AVC 1.10 GB

🎬 某电影.2024.2160p.UHD.BluRay.Remux
   2160p · 杜比视界 · 原盘 · TrueHD · OurBits
   🔗 下载

🕒 09-30 08:16
```

自动识别并展示的规格：**分辨率**（4320p/2160p/4K/1080p/720p…）、**HDR**（杜比视界/HDR10+/HDR10）、
**视频编码**（HEVC/AVC/AV1）、**来源**（原盘/BluRay/WEB-DL/HDTV）、**音轨**（TrueHD/Atmos/DTS-HD/DDP/FLAC）、
**字幕语言**（国语/粤语/中字/双语）、**压制组**，以及内容类型（剧集📺/电影🎬/合集📦/音乐🎵/图书📚/软件💿）。

识别原则是**宁缺勿错**：认不出来就不写标签（免得你按 `2160p` 过滤时发现标错了）。
默认只展示前 5 个标签，避免一行太长。

---

## 4. 「入库集数 / 全部集数」是怎么算的

以订阅《进击的巨人》为例：

| 环节 | 取数来源 | 结果 |
|---|---|---|
| 分母 | TMDB `/tv/{id}/season/{n}` 的集列表，按 `air_date <= 今天` 过滤 | `87` |
| 分子 | Emby/Jellyfin `/Shows/{id}/Episodes` 返回的 `(ParentIndexNumber, IndexNumber)` 集合 | `83` |
| 差值 | 逐集比对，缺失的压缩成区间 | `S04E84-S04E87` |
| 推送 | `███████░ 83/87 集（95%）｜待入库 S04E84-S04E87` | |
| 完成 | 分子 ≥ 分母 → 推送「订阅完成」→ 回写 YAML 删除订阅 | ✅ |

匹配媒体库里的剧时按顺序尝试：**TMDB ID → TVDB ID → 剧名+年份打分**，所以只要 Emby 里刮削过，基本不会认错。

### 集号识别规则

RSS 标题 → 集号，按优先级：

1. `S02E05` / `s2e5` / `2x07`
2. `第05集` / `第12话` / `第二季`
3. `EP05` / `E05`
4. `[Sub] 番剧名 - 07 [1080p]`（动漫绝对集数）
5. `全12集`（合集）

`2160p` / `1080p` 这类分辨率不会被误认成集号（有专门的反例测试）。

---

## 5. 命令行

容器内执行（`docker compose exec rss-media-hub python -m app <命令>`）：

| 命令 | 作用 |
|---|---|
| `run` | 常驻运行（容器默认命令） |
| `check [订阅id] [--notify]` | 立刻做一次入库比对，输出到终端；`--notify` 同时推 TG |
| `scan [--limit N] [--json] [--export 路径] [--show-complete] [--refresh]` | **扫描整个媒体库**，列出每部剧「入库 x / 全部 y」与缺集报告 |
| `gaps [--limit N] [--json] [--export 路径] [--scan-only] [--refresh]` | **查漏**：库里缺的集，当前能不能在 RSS 里找到 |
| `feeds [--json] [--export 路径]` | **订阅源体检**：每个 RSS 能不能抓、抓到多少、最新几条长什么样 |
| `selfcheck [--json]` | **部署自检**：目录权限、订阅表、模板、服务配置全套检查（离线可用） |
| `preflight [--notify] [--json]` | **连通性预检**：真的连一次 TG / TMDB / Emby，确认"第一次推送"能不能成 |
| `list` | 列出所有订阅、最近一次巡检结果、登记条目数 |
| `add "剧名" --rss <地址> --tmdb-id <id> [--year] [--season]` | 新增订阅并写回 YAML |
| `rm <订阅id或名称> [--keep-items]` | 删除订阅并清理状态 |
| `test-notify` | 发一条 Telegram 测试消息 |
| `web` | 只跑网页 UI（不启动轮询/巡检循环），用于调试界面 |

### 5.1 网页 UI

服务启动后浏览器打开 **`http://<NAS IP>:18080/`**，四个标签页：

| 标签 | 能做什么 |
|---|---|
| **总览** | 追更进度一览、缺哪些集、TG/TMDB/Emby 连通状态、轮询与推送统计、上次扫描结果 |
| **订阅** | 增删改订阅（表单填写，自动写回 `subscriptions.yaml` 并立即生效）；RSS 里的 passkey 会打码显示 |
| **媒体库** | 一键全库扫描，按状态筛选（缺集 / 未入库 / 需处理 / 完整）；查漏并列出可下的资源 |
| **设置** | 图形化配置 Telegram / TMDB / Emby / 运行参数，**保存即热重载，不用重启容器** |

几个实现上的要点：

- **密钥只进不出**：接口返回的 Token / API Key 一律打码成 `abcd********wxyz`，留空表示"不修改"
- **配置分层**：界面写入 `config/settings.yaml`，**不会动你手写的 `config.yaml`**；优先级是 环境变量 > settings.yaml > config.yaml
- **环境变量优先**：被 `.env` / compose 环境变量控制的字段会标上 `env` 标记，说明改了也不生效
- **字段白名单**：界面只能改登记过的字段，其他一律拒绝（避免写坏配置文件）
- **换 Token/地址会重建客户端**：保存后立刻生效，不需要重启

> ⚠️ **务必设置 `RMH_UI_TOKEN`**。不设的话，同一局域网内任何人都能打开界面看到你的配置并修改。
> 也可以设 `RMH_UI_ENABLED=false` 彻底关掉界面。

### 5.2 部署自检（`selfcheck`）

**部署完第一件事跑这个**。它逐项检查最容易出错的地方，并且每条都告诉你**具体怎么修**：

```bash
docker exec rss-media-hub python -m app selfcheck
```

```
 ✅ 配置 目录              可写：/config
 ✅ 状态 目录              可写：/state
 ✅ 订阅表                1 条订阅（全量 1 · 按剧追踪 0）
 ✅ 订阅的 RSS 地址         所有启用的订阅都有 RSS 地址
 ✅ 状态数据库             /state/rss-media-hub.db（36 KB）— 已有历史记录
 ✅ 入口脚本              结构正常（set -e、heredoc 配对、会降权执行）
 ✅ 首次启动模板           可解析，包含 1 条示例订阅
 ❌ Telegram 配置        缺少：RMH_TG_BOT_TOKEN
                        ↳ 找 @BotFather 拿 Token；chat id 找 @userinfobot
 ⚠️  Emby/Jellyfin 配置  未配置（只影响「按剧追踪」与「查漏」，feed 全量模式照常工作）
 ⚠️  网页 UI             已启用但没设访问口令
                        ↳ 同一局域网内任何人都能打开界面并改你的配置

结论：有 1 项必须处理（Telegram 配置），否则对应功能用不了。
```

**它检查 12 项**：版本与运行身份、config/state 目录**真实可写性**（会真的写个文件再删掉，不只看权限位）、
订阅表可解析性、每条订阅的 RSS / `tmdb_id` 完整性、状态数据库与磁盘空间、镜像入口脚本结构与首次启动模板、
三个外部服务的配置齐全度、网页 UI 口令。

**完全离线，0.01 秒出结果**——断网也能跑，适合部署时第一时间自检。
有 `FAIL` 时退出码为 `1`，方便写脚本。网页「总览」页也有「立即自检」按钮。

### 5.3 连通性预检（`preflight`）

`selfcheck` 只查"配置填没填"，`preflight` **真的去连一次**，确认三个外部服务都能用：

```bash
docker exec rss-media-hub python -m app preflight            # 只连不发（推荐）
docker exec rss-media-hub python -m app preflight --notify    # 顺便真发一条 TG 测试消息
```

```
 ✅ 预检模式         会真实访问外部服务（不会发送推送）
 ✅ Telegram 连通    机器人 @your_bot（Your Name）
 ✅ TMDB 连通        查询成功：《进击的巨人》4 季 / 87 集
 ✅ 媒体服务器连通    emby 4.9.5.0 @ http://192.168.31.221:8096
 ✅ 媒体库可读        读到 5 部剧（示例：进击的巨人），已刮削 TMDB ID
 ✅ 订阅源          1 条启用的订阅都有 RSS

结论：全部通过，可以正常运行。
```

**失败时会直接告诉你怎么修**，这是它最大的价值：

| 现象 | 提示 |
|---|---|
| Telegram 连接失败 | 国内直连通常不通，填正向代理 `RMH_TG_PROXY` |
| Telegram 发送失败 | chat id 填错、机器人没被你 `/start` 过、或群里没给它发言权限 |
| TMDB 查询失败 | 填 `RMH_TMDB_PROXY`（正向代理）或 `RMH_TMDB_API_BASE`（自建反代） |
| Emby 连接失败 | ① 地址必须是 NAS 局域网 IP，**不能用 localhost**（容器里的 localhost 是容器自己）② 密钥是否有效 ③ 自签证书要 `RMH_EMBY_VERIFY_TLS=false` |
| 媒体库一部剧都没读到 | 接口通了但库是空的，或密钥对应用户无权访问 |

> **默认不发推送**，只调只读接口；只有加 `--notify` 才真的发消息。
> 预检刻意用**更短的重试与超时**（1 次 / 5-8 秒，轮询时是 3 次 / 20 秒），
> 所以服务不可达时约 10 秒就出结果，而不是让您等一分多钟。

网页「总览」页有「离线自检」和「连通性预检」两个按钮。

### 5.4 订阅源体检（`feeds`）

**配 RSS 之后第一件事就该跑这个**——不需要配 Emby/TMDB，直接告诉你每个源能不能用：

```bash
docker exec rss-media-hub python -m app feeds
```

```
共 1 个源：✅ 正常 0 个，❌ 失败 1 个，耗时 0.0s

❌ [我的 PT 站] https://your-pt-site.example/rss?passkey=***
     ConnectError: [Errno 11001] getaddrinfo failed —— 域名解析失败：检查地址拼写或 DNS

---- 排错建议 ----
  · 401/403：passkey 过期或地址不完整，去站点重新复制 RSS 地址
  · 404：地址路径写错，或该分类已被站点移除
  · 超时/连接失败：NAS 访问不了该站点，需要配 RMH_PROXY 走代理
  · 返回 HTML 而不是 XML：多半被 CF 盾拦了，或需要登录态
```

正常时会显示每个源抓到了多少条、最新几条，并且**带规格标签**：

```
✅ [我的 PT 站] https://pt.example/rss?passkey=***
     37 条 · 0.42s · 最新 02-05 20:00
       S01E05  12.50 GB  2160p·HDR·HEVC   某剧.S01E05.2160p.WEB-DL.HDR.HEVC-FGT
       S01E04  3.20 GB   1080p·AVC         某剧.S01E04.1080p.WEB-DL.H264-YYY
```

网页上的「总览」标签页也有「订阅源体检 → 一键检查」按钮，效果一样。

> URL 里的 passkey 会自动打码成 `***`，贴给别人看也不会泄露。
> 有源失败时退出码是 `2`（正常为 `0`），方便写脚本判断。

### 5.5 媒体库全库扫描（`scan`）

反向摸清"库里到底缺什么"——这是后续「查漏模式」的基础。

```bash
docker exec rss-media-hub python -m app scan                 # 人看的报告
docker exec rss-media-hub python -m app scan --show-complete # 连已完整的也列出来
docker exec rss-media-hub python -m app scan --json | jq .summary
docker exec rss-media-hub python -m app scan --export /state/report.json
docker exec rss-media-hub python -m app scan --limit 50       # 先扫描 50 部试水
```

输出长这样：

```
==============================================================================
媒体库扫描报告
==============================================================================
库内剧集 128 部，本次检查 128 部，耗时 41.2s
TMDB 请求 96 次，缓存命中 32 次

✅ 完整   74 部    ⏳ 缺集   38 部    🕐 未入库    9 部
🆕 未播出    5 部    ❓ 未匹配    2 部    ❌ 出错    0 部

---- 缺集最多的 15 部（共缺 213 集）----
    83/87   95.4%  进击的巨人（2013）  缺 S04E84-S04E87
    54/62   87.1%  某部剧（2019）  缺 S03E55-E62
     0/12    0.0%  无职转生（2021）
```

关于它的三点说明：

- **分母默认只算「已播出」**（跟随 `RMH_EMBY_COUNT_AIRED_ONLY`），否则连载中的剧永远显示不满
- **结果会缓存到 SQLite**（默认 12 小时）：第二次扫描几乎不打 TMDB，库大的时候很关键；想强制刷新加 `--refresh`
- **匹配不到 TMDB 的剧**会标成 `❓ 未匹配` 单列出来，通常是 Emby 里没刮削出 TMDB ID，去 Emby 里「识别」一次即可

网页上扫描完成后可以按状态筛选：**缺集 / 未入库 / 需处理（未匹配+出错）/ 完整**。
「需处理」这栏最值得定期看一眼——里面的剧通常只是 Emby 没刮削好，修好后进度就准了。

### 5.6 查漏：缺的集现在能下吗（`gaps`）

`scan` 只告诉你"缺什么"，`gaps` 进一步告诉你"**现在能不能下到**"：把库里缺的集，拿去和你 RSS 源里当前的条目做交叉比对。

```bash
docker exec rss-media-hub python -m app gaps                # 先扫库再比对（第一次较慢）
docker exec rss-media-hub python -m app gaps --scan-only     # 复用上次扫描结果，只抓 RSS（快）
docker exec rss-media-hub python -m app gaps --json | jq .summary
docker exec rss-media-hub python -m app gaps --export /state/gaps.json
```

输出长这样：

```
扫了 2 部有缺集的剧，共缺 5 集；其中 1 集当前能在 RSS 里找到
RSS：1 个源成功 / 0 个失败，共 3 条条目，耗时 6.2s

---- 有货可下的 1 部 ----
  进击的巨人（2013）  缺 3 集，现成 1 集
      S04E85     1.30 GB  [组] 进击的巨人 - 85 [1080p]

---- 暂时没货的 1 部 ----
  某部剧（2021）  缺 2 集：S01E09、S01E10
```

网页上在「媒体库」标签页，点「开始查漏」就行，有货的集会直接列出下载链接。

> ⚠️ **必须理解这个能力边界**：RSS 只包含站点最近的一批更新（通常几十条），**不是全站索引**。
> 所以 `gaps` 只能发现"缺的集**恰好还在 RSS 窗口里**"的情况。
> 已经翻页过去的旧集，靠读 RSS 永远找不到——那需要调用站点的**搜索接口**（本工具目前不做，因为各站差异太大）。

匹配规则（**宁可多报，不轻易漏报**）：

| RSS 条目 | 库里缺口 | 是否算命中 | 原因 |
|---|---|---|---|
| `S04E85` | `S04E85` | ✅ | 季集完全一致 |
| `番剧名 - 85`（绝对集数，无季号） | `S04E85` | ✅ | 字幕组常用绝对集数，按集号匹配 |
| `番剧名 - 85`（绝对集数） | `S04E85` | ✅ | 同上；不确定的交给人工判断 |
| `S01E85` | `S04E85` | ❌ | 两边都有季号且不一致，认为不是同一集 |
| `全12集`（合集） | 任意 | ❌ | 解析不出具体集号 |

每条缺口可能有多个候选资源，会自动挑**带直链（PT 站 enclosure 带 passkey，可直接下）且体积最大**的那个作为推荐。

---

## 6. 部署细节

### 6.1 绿联云（UGREEN NAS）图形界面部署

用仓库根目录的 [`compose.yaml`](compose.yaml)：**不需要源码、不需要 build**，整段粘进绿联云 Docker 的「项目 / Compose」，只改 4 处标了【必改】的值就能跑。

**前置条件**：先把镜像弄进绿联云的本地镜像列表（三选一）

```bash
# 方案 A：在能装 Docker 的电脑上构建后导出 tar，绿联云「镜像 → 导入」选这个文件
docker build -t rss-media-hub:1.0.0 .
docker save rss-media-hub:1.0.0 -o rss-media-hub-1.0.0.tar

# 方案 B：推到你自己的仓库，绿联云直接拉
docker tag rss-media-hub:1.0.0 <你的仓库>/rss-media-hub:1.0.0
docker push <你的仓库>/rss-media-hub:1.0.0

# 方案 C：绿联云能联网 → 在绿联云「终端」里从仓库拉取
docker pull <你的仓库>/rss-media-hub:1.0.0
```

**然后在绿联云界面里**：

1. Docker → 项目 → 新建 → 粘贴 `compose.yaml` 全文
2. 改 4 处「必改」：
   - ① `image:` 换成你实际的镜像名（方案 A 就填 `rss-media-hub:1.0.0`）
   - ② Telegram 机器人 Token + chat id
   - ③ TMDB API Key
   - ④ Emby 地址（**用 NAS 的局域网 IP，不要用 localhost**）+ API 密钥
3. 存储目录：默认是 `/volume1/docker/rss-media-hub/config` 与 `/state`，按你实际想放的位置改；**这两个目录建议先建好**（config 目录需要可写，因为程序要回写订阅表）
4. 部署 → 看日志，出现 `Telegram 机器人：@xxx` 即成功

**之后怎么改订阅**：编辑存储目录里的 `config/subscriptions.yaml`，30 秒内自动热重载，不用重启容器：

```yaml
subscriptions:
  - id: some-show
    name: 某部剧
    tmdb_id: 12345
    rss: https://pt.example/rss?passkey=你的PASSKEY&cat=2
    quality: ["1080p", "2160p"]
    exclude_filter: "预告|花絮|OST"
    remove_when_done: true
```

**排查接口**（同局域网浏览器直接开）：
- `http://<NAS IP>:18080/healthz` → `ok`
- `http://<NAS IP>:18080/status` → 每条订阅的实时进度 JSON

**如果日志报 `Permission denied`**：容器内程序以 uid 1000 运行，而你的存储目录属主不是 1000。在绿联云终端执行 `ls -ln /volume1/docker/rss-media-hub` 看第三、四列数字，把 `compose.yaml` 里的 `RMH_UID` / `RMH_GID` 改成那两个数字，重新部署即可。

### 6.2 通用部署细节

- **数据持久化**：`config/`（订阅表，程序会回写）+ `state/`（SQLite 去重记录）。
  删掉 `state/` 会导致历史 RSS 条目被重新推送一次。
- **权限**：容器入口脚本会先用 root 建目录并把 `/config`、`/state` 的属主改成 `RMH_UID:RMH_GID`（默认 `1000:1000`），然后降权运行。如果你的 NAS 不允许改属主，把这两个变量设成目录现有属主编号即可。
- **Emby 在宿主机本机**：compose 里已留 `extra_hosts: emby.local:host-gateway` 注释，取消注释后用 `emby.local` 当地址。
- **自签证书**：把 CA 证书放到 `config/certs/ca.pem`，compose 里挂 `/certs:ro` 并设 `SSL_CERT_FILE`、`RMH_EMBY_VERIFY_TLS=false`。
- **健康检查**：`GET /healthz` → `ok`；`GET /status` → JSON（订阅数、进度、上次错误）。
- **两个 compose 文件的分工**：

  | 文件 | 用途 | 特点 |
  |---|---|---|
  | [`compose.yaml`](compose.yaml) | 绿联云等 NAS 图形界面 | 不 build、环境变量全在界面上、绝对路径挂载 |
  | [`compose.ghcr.yaml`](compose.ghcr.yaml) | 已配置 GitHub Actions 后 | 直接拉 `ghcr.io/...:latest`，升级只需 `docker compose pull` |
  | [`docker-compose.yml`](docker-compose.yml) | 自己有源码的机器 | 带 `build:`、密钥走 `.env`、state 用命名卷 |

- **相关文档**：
  - [`验收清单.md`](验收清单.md) —— **部署后照着勾**，每项都写了期望结果与排错办法
  - [`NAS-构建与部署.md`](NAS-构建与部署.md) —— 在绿联云终端从源码构建（路线 B）
  - [`NAS-绿联云部署.md`](NAS-绿联云部署.md) —— 用导出的 tar 在绿联云部署
  - [`GITHUB推送指南.md`](GITHUB推送指南.md) —— 推到 GitHub 让镜像自动构建

---

## 7. 常见问题

**Q：一条 TG 都没收到？**
先跑 `python -m app test-notify`。如果报 401，检查 Token；如果一直超时，多半是网络问题。

**Q：正向代理（科学上网）和反向代理怎么区分？我该填哪个？**

| | 填什么变量 | 值的形态 | 什么时候用 |
|---|---|---|---|
| **正向代理** | `RMH_TG_PROXY` / `RMH_TMDB_PROXY` | `http://192.168.31.142:10809`、`socks5://192.168.31.142:10808` | 你有个科学上网的代理端口（clash/v2ray 等），想让程序借它出去 |
| **反向代理** | `RMH_TG_API_BASE` / `RMH_TMDB_API_BASE` | `https://tgapi.example.com`、`https://tmdb.example.com` | 你自己用域名反代了 TG/TMDB 的 API |

**最常见的错误**：把科学上网的代理端口填进 `RMH_TG_API_BASE`。那样请求会变成
`http://192.168.31.142:10809/bot<token>/sendMessage`——变成"给代理服务器发 HTTP 请求"，必然失败。
**有代理端口就该填 `RMH_TG_PROXY`。**

另外：Emby 在局域网内，**不要**给它配代理（`RMH_EMBY_PROXY` 留空），否则可能连不上内网。

**Q：媒体库里明明有，却显示「未入库」？**
按顺序排查：① `RMH_EMBY_URL` 从容器里能不能访问（`docker compose exec rss-media-hub python -c "import httpx;print(httpx.get('http://你的地址:8096/System/Info/Public').status_code)"`）；② 订阅名与 Emby 里的剧名差异太大 → 补 `tmdb_id`；③ Emby 里那部剧没刮削出 TMDB ID → 手动在 Emby 里「识别」一次。

**Q：分母太大，永远追不完？**
保持 `RMH_EMBY_COUNT_AIRED_ONLY=true`（默认），分母只算已播出集数。如果连未播出的也算进分母，订阅完成后又要等下一集，就不会退订。

**Q：字幕组用绝对集数（`- 07`），但 TMDB 是 S01E07？**
如果 TMDB 上该番就是「1 季 N 集」，直接匹配没问题。如果 TMDB 分季而字幕组用绝对集数，在订阅里写 `season: 1` 并只订阅该季，或用 `name_filter` 精确圈定。

**Q：为什么升级/换机器后 TG 又推了一遍历史条目？**
`state/` 卷丢了。用命名卷（compose 默认）就不会丢。

**Q：想临时关掉某条订阅？**
在 `subscriptions.yaml` 里给它加 `enabled: false`，30 秒内生效。

**Q：能同时订阅两部同名剧吗？**
能，用不同的 `id` 和各自的 `tmdb_id`。

---

## 8. 开发与测试

```bash
# 本地跑（不用 Docker）
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export RMH_CONFIG_DIR=./config RMH_STATE_DIR=./state
cp .env.example .env && set -a && . ./.env && set +a
python -m app check
python -m app run

# 测试（30 个用例，全部离线，不需要网络/密钥）
python -m unittest discover -s tests -t .
```

测试覆盖：

| 文件 | 覆盖内容 |
|---|---|
| `tests/test_rss.py` | RSS/Atom 解析、magnet/enclosure 提取、体积换算、集号识别（含 `2160p` 反例） |
| `tests/test_config.py` | YAML 读写往返、订阅规则（含 PyYAML 缺失时的内置解析器）、环境变量优先级 |
| `tests/test_integration.py` | 端到端：首轮静默 → 新种推送 → 入库通知 → 追完退订 → 停机恢复 → 出错不崩 |

### 目录结构

```
app/
  cli.py        命令行入口（run/check/scan/list/add/rm/test-notify）
  main.py       编排层：轮询循环、巡检循环、健康检查、退订
  config.py     配置加载（含无 PyYAML 时的内置迷你 YAML 解析器）
  db.py         SQLite 状态：条目去重、订阅进度、TMDB 缓存、KV
  rss.py        RSS/Atom 抓取与解析、集号与体积识别
  release.py    发布信息提取：分辨率/HDR/编码/来源/字幕/压制组 + 内容类型判断
  tmdb.py       TMDB 客户端：季集结构与播出日期
  emby.py       Emby/Jellyfin 客户端：已入库剧集查询、全库枚举
  reconcile.py  单剧比对：分母/分子/缺失区间
  libraryscan.py 全库扫描：反向枚举媒体库 → 逐部算缺口 → 出报告
  gapfill.py     查漏：把库里缺的集和当前 RSS 里的条目对上
  feedcheck.py   订阅源体检：并发检查每个 RSS，把失败原因翻译成"该怎么办"
  telegram.py   Telegram 发送（HTML 排版、长消息切分、限流重试）
  notify.py     消息排版（信息卡、同集多版本合并、发布规格标签）
  webserve.py   网页 UI 的 HTTP 服务与 REST API
  webui.py      单页界面（HTML/CSS/JS 内联，含深色/浅色主题）
docker/
  entrypoint.sh 容器入口：初始化目录/属主 → 降权执行
tests/          离线测试（49 个用例，不需要网络与密钥）
```

---

## 9. 许可证

MIT
