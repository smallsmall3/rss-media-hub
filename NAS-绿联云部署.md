# 绿联云部署速查（照着做，5 分钟）

> 目标：把镜像放进绿联云，用 `compose.yaml` 起一个容器，改 4 个值就能跑。

---

## 第 0 步：先想清楚镜像怎么进去

镜像必须在绿联云的本地镜像列表里，`compose.yaml` 才部署得起来。走方案 A：

```powershell
# 在你的 Windows 电脑上，进到项目根目录
powershell -ExecutionPolicy Bypass -File .\build-image.ps1
```

脚本会自动：检查 Docker → 构建 → 导出 `rss-media-hub-1.0.0.tar` → 打印后续步骤。

- 首次构建约 2-5 分钟，tar 约 200-300 MB
- 绿联云绝大多数机型是 x86_64，Windows 电脑构建出来的就是 amd64，**直接匹配，不用加参数**
- 只有你的绿联云是 ARM 机型时，才需要 `-Platform linux/arm64` 重新导出

---

## 第 1 步：导入镜像

1. 把 `rss-media-hub-1.0.0.tar` 传到绿联云（绿联云「文件」上传，或直接拷进共享文件夹）
2. 绿联云 → **Docker** → **镜像** → **本地镜像** → **导入** → 选中这个 tar
3. 等 1-3 分钟，列表里出现 `rss-media-hub:1.0.0` 即成功

---

## 第 2 步：建存储目录

在绿联云「文件」里建两个文件夹，比如：

```
/volume1/docker/rss-media-hub/config
/volume1/docker/rss-media-hub/state
```

- `config` 放订阅表（程序要**回写**，所以必须可写）
- `state` 放 SQLite 去重记录

> 懒人做法：两个都不用手动建，`compose.yaml` 里路径写好后部署时 Docker 会自动建，入口脚本还会顺手把属主改成容器用的 uid（默认 1000）。

---

## 第 3 步：建项目

1. 绿联云 → **Docker** → **项目** → **新建**
2. 项目名随便填（如 `rss-media-hub`）
3. 把项目里 [`compose.yaml`](compose.yaml) 的内容**整段粘贴**进去
4. 按下表改 4 处，其余保持默认

| 要改的地方 | 改成什么 | 去哪拿 |
|---|---|---|
| ① `image:` | `rss-media-hub:1.0.0` | 第 1 步导入的名字 |
| ② `RMH_TG_BOT_TOKEN`<br>`RMH_TG_CHAT_ID` | 机器人 Token<br>你的 chat id | Token 找 [@BotFather](https://t.me/BotFather) 发 `/newbot`<br>chat id 找 [@userinfobot](https://t.me/userinfobot) |
| ③ `RMH_TMDB_API_KEY` | 32 位字符串 | <https://www.themoviedb.org/settings/api> 用 **API Key (v3 auth)** |
| ④ `RMH_EMBY_URL`<br>`RMH_EMBY_API_KEY` | `http://192.168.1.x:8096`<br>API 密钥 | Emby：后台 → 高级 → API 密钥 → 新建<br>Jellyfin：控制台 → API 密钥 → 新建 |

5. 存储目录那两行改成你第 2 步的真实路径
6. 部署 → 看日志

**看到这一行就成功了**：

```
INFO  rss-media-hub | Telegram 机器人：@你的机器人名
```

---

## 第 4 步：加订阅

编辑 `/volume1/docker/rss-media-hub/config/subscriptions.yaml`（这个文件首次启动会自动生成，里面带完整注释）：

```yaml
subscriptions:
  - id: some-show
    name: 某部剧
    tmdb_id: 12345                                    # TMDB 剧集页 URL 中间那串数字
    rss: https://pt.example/rss?passkey=你的PASSKEY&cat=2
    quality: ["1080p", "2160p"]
    exclude_filter: "预告|花絮|OST"
    remove_when_done: true                            # 追完自动删掉这条订阅
```

**保存后 30 秒内自动生效，不用重启容器。**

---

## 第 5 步：验收

在同一个局域网用浏览器打开：

| 地址 | 看到什么 |
|---|---|
| `http://<NAS IP>:18080/healthz` | `ok` |
| `http://<NAS IP>:18080/status` | 每条订阅的实时进度 JSON |

---

## 出问题先看这里

| 现象 | 原因 / 解决 |
|---|---|
| 部署报 `pull access denied`、`image not found` | 第 1 步没做完，或 `image:` 名字和导入的名字不一致（大小写、tag 都要对） |
| 容器起来就退出，日志 `exec format error` | 镜像架构和 NAS 不一致，用 `-Platform linux/arm64` 或 `linux/amd64` 重新导出 |
| 日志 `Permission denied` | 在绿联云终端 `ls -ln /volume1/docker/rss-media-hub`，把第三、四列数字填到 `RMH_UID` / `RMH_GID`，重新部署 |
| 一条 TG 都没收到 | 在绿联云终端执行 `docker exec rss-media-hub python -m app test-notify`，按提示看是 401 还是网络超时 |
| 显示「未入库」但 Emby 里明明有 | ① `RMH_EMBY_URL` 别写 localhost，要用 NAS 局域网 IP ② 给订阅补 `tmdb_id` |
| 想强制做一次比对 | `docker exec rss-media-hub python -m app check` |
| 想临时停掉某条订阅 | 给那条加 `enabled: false` |
