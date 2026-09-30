# 绿联云构建与部署（路线 B：在 NAS 上直接构建）

> **适合你**：电脑上没装 Docker，而绿联云自带 Docker。
> **好处**：不用在电脑装 Docker Desktop，也不用导出/导入 tar（省掉 250MB 传输）。
> **代价**：要把 80KB 源码传到 NAS，并在 NAS 终端敲一条构建命令。

---

## 0. 准备

把打包好的 **`rss-media-hub-src.zip`**（约 80KB）传到绿联云。
用绿联云「文件」网页上传即可，或拷进任意共享文件夹。

---

## 1. 解压到 docker 目录

在绿联云「文件」里：

1. 进入你想放的位置，例如 `/volume1/docker/`
2. 新建文件夹 `rss-media-hub-src`
3. 把 `rss-media-hub-src.zip` 上传进去
4. 右键 zip → **解压到当前目录**

解压后应该是这样（`Dockerfile` 必须在这一层）：

```
/volume1/docker/rss-media-hub-src/
├── Dockerfile          ← 构建入口，必须在这
├── requirements.txt
├── compose.yaml
├── app/
├── docker/
└── ...
```

> 如果解压出来多套了一层（变成 `rss-media-hub-src/rss-media-hub/Dockerfile`），
> 就把里层的内容整体移到上一层，或者构建命令里的路径改成里层那个。

---

## 2. 打开终端，确认路径

绿联云的终端入口：**控制面板 → 终端服务**（开启 SSH），然后用电脑上的 SSH 工具连，
用户名/密码和你登录绿联云的一致；端口默认 22。

```bash
# 连上之后先确认 Docker 可用
docker --version
docker compose version
```

然后进到源码目录。**这里要找对真实路径**，`/volume1` 只是常见写法，用 `ls` 确认：

```bash
ls /                          # 看看根目录下有哪些卷
ls /volume1/docker            # 看你的源码目录在不在
cd /volume1/docker/rss-media-hub-src
ls -l Dockerfile              # 能列出文件就说明路径对了
```

---

## 3. 构建镜像（核心就这一步）

```bash
cd /volume1/docker/rss-media-hub-src
docker build -t rss-media-hub:1.0.0 .
```

- **别漏掉最后那个 `.`**（表示用当前目录当构建上下文）
- 首次约 2-5 分钟，会从网上下载 python 基础镜像和依赖
- 看到 `Successfully tagged rss-media-hub:1.0.0` 就成了

验证：

```bash
docker images | grep rss-media-hub
# 应该有一行：rss-media-hub   1.0.0   xxxxx   1分钟前   约 200MB
```

**如果卡在下载依赖**（国内网络常见），先给 NAS 的 Docker 配个镜像加速器，或者从能联网的机器构建后导 tar（就是路线 A）。

---

## 4. 建存储目录

```bash
mkdir -p /volume1/docker/rss-media-hub/config
mkdir -p /volume1/docker/rss-media-hub/state
```

> 不建也行：部署时 Docker 会自动创建，入口脚本还会顺手把属主改成容器用的 uid（默认 1000）。

---

## 5. 用 compose 部署

推荐用绿联云的图形界面（以后改配置方便）：

1. 绿联云 → **Docker** → **项目** → **新建**
2. 粘贴源码目录里 `compose.yaml` 的全文
3. 改这 4 处：

| 位置 | 改成 |
|---|---|
| `image:` | `rss-media-hub:1.0.0` （本地已构建的镜像名） |
| `RMH_TG_BOT_TOKEN` / `RMH_TG_CHAT_ID` | Telegram 机器人 Token / 你的 chat id |
| `RMH_TMDB_API_KEY` | TMDB 的 API Key (v3 auth) |
| `RMH_EMBY_URL` / `RMH_EMBY_API_KEY` | `http://内网IP:8096` + API 密钥 |

4. 存储目录改成第 4 步的真实路径
5. 部署

**看到这行就成功了**：

```
INFO  rss-media-hub | Telegram 机器人：@你的机器人名
```

> 想在终端里部署也行：`cd /volume1/docker/rss-media-hub-src && docker compose -f compose.yaml up -d`
> 注意这样起的话，`compose.yaml` 里的相对路径不适用——它用的是绝对路径（`/volume1/...`），所以没问题。

---

## 6. 加订阅

编辑 `/volume1/docker/rss-media-hub/config/subscriptions.yaml`（首次启动会自动生成，带完整注释）：

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

**保存后 30 秒内自动生效，不用重启容器。**

---

## 7. 验收

```bash
curl http://127.0.0.1:18080/healthz      # 期望输出 ok
docker logs --tail 50 rss-media-hub      # 看运行日志
docker exec rss-media-hub python -m app check    # 立刻做一次入库比对
docker exec rss-media-hub python -m app list     # 看所有订阅进度
```

浏览器打开 `http://<NAS IP>:18080/status` 还能看到 JSON 格式的实时进度。

---

## 常见问题

| 现象 | 原因 / 解决 |
|---|---|
| `docker: command not found` | 终端没连对，或绿联云 Docker 服务没启动 |
| `unable to prepare context: path not found` | 你不在源码目录里，或最后漏了 `.` |
| `Cannot locate specified Dockerfile` | `Dockerfile` 不在当前目录（多半是解压多套了一层） |
| 构建很慢 / 卡住 | 网络问题，配 Docker 镜像加速器；或改用路线 A（电脑构建导 tar） |
| 部署报 `image not found` | 第 3 步没构建成功，或 `image:` 名字和构建时的 tag 不一致 |
| 日志 `exec format error` | 绿联云是 ARM 机型，需要换 ARM 机器构建（或加 `--platform`） |
| 日志 `Permission denied` | `ls -ln /volume1/docker/rss-media-hub` 看第三四列数字，填到 `RMH_UID`/`RMH_GID` |
| 改了订阅不生效 | 确认改的是**存储目录**里的 `config/subscriptions.yaml`，不是源码目录里的 |
