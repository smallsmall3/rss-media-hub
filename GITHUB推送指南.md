# 推到 GitHub，让镜像自动构建

> 目标：`git push` 之后 GitHub Actions 自动构建镜像并推到 `ghcr.io`，
> 绿联云上的 compose 直接写 `image: ghcr.io/你的用户名/rss-media-hub:latest` 就能拉取。
> 以后升级只要 `docker compose pull && docker compose up -d`。

---

## 1. 在 GitHub 建仓库

1. 打开 <https://github.com/new>
2. Repository name 填 `rss-media-hub`
3. **选 Public**（这样 ghcr 镜像也能设为公开，NAS 拉取不用登录）
4. **不要**勾选 Add README / .gitignore / license（本地已经有了）
5. Create repository

---

## 2. 本地初始化并推送

在项目根目录（`rss-media-hub` 文件夹）执行。**先确认 `.env` 没被跟踪**：

```powershell
cd C:\Users\92435\Documents\deepseek-harness\default-workspace\rss-media-hub

git init
git branch -M main

# 关键一步：确认敏感文件被忽略
git status --short
# .env / config/subscriptions.yaml / state/ 都不应该出现在输出里
# 如果出现了，先检查 .gitignore 再继续

git add .
git commit -m "feat: RSS 订阅 + Emby 入库比对 + Telegram 推送"

git remote add origin https://github.com/<你的用户名>/rss-media-hub.git
git push -u origin main
```

推上去之后 Actions 会自动跑 `Tests`（两个 Python 版本），几十秒出结果。

---

## 3. 打 tag 触发镜像构建

```powershell
git tag v1.0.0
git push origin v1.0.0
```

然后去 GitHub 仓库 → **Actions** → 看 `Release Image` 这个 workflow：

```
test              ✅   （先跑测试，不过就不构建镜像）
build-and-push    ✅   linux/amd64, linux/arm64
```

跑完后镜像地址是：

```
ghcr.io/<你的用户名>/rss-media-hub:latest
ghcr.io/<你的用户名>/rss-media-hub:v1.0.0
```

> **用户名必须小写**。GitHub 用户名本身是小写；但如果你是组织（Organization），
> 名字可能含大写，而 ghcr 只接受小写——workflow 里已经用 `${VAR,,}` 自动转小写了，
> 你填 compose 时照着小写写即可。

---

## 4. 把镜像设为公开（重要）

**不设的话，NAS 上拉取会报 `unauthorized`，需要 `docker login ghcr.io`。**

1. GitHub → 你的头像 → **Your packages**（或仓库首页右侧 Packages）
2. 点 `rss-media-hub`
3. 右侧 **Package settings** → 最下面 **Danger Zone** → **Change visibility** → **Public**
4. 输入包名确认

---

## 5. 绿联云上切换到这个镜像

把仓库里的 [`compose.ghcr.yaml`](compose.ghcr.yaml) 内容粘到绿联云 Docker → 项目 → 新建，
改掉里面 4 处【必改】和 `<你的GitHub用户名>`，部署。

之后升级流程就变成了：

```bash
# NAS 终端里（或绿联云界面上点"重新拉取/更新"）
cd /volume1/docker/rss-media-hub
docker compose pull
docker compose up -d
```

---

## 常见问题

| 现象 | 原因 / 解决 |
|---|---|
| Actions 报 `denied: permission_denied` | 仓库 Settings → Actions → General → Workflow permissions 选 **Read and write permissions** |
| NAS 拉取报 `unauthorized` | 第 4 步没做，镜像还是私有 |
| NAS 拉取报 `manifest unknown` | 打 tag 后 workflow 还没跑完，或用户名大小写写错了 |
| `docker compose pull` 提示已是最新 | 正常，说明镜像没更新；打了新 tag 才会有变化 |
| 想用固定版本而不是 latest | 把 `image:` 改成 `...:v1.0.0`，升级时手动改版本号 |
| GitHub 连不上 / 太慢 | 用 `ghproxy` 之类的加速，或改用阿里云容器镜像服务的个人版仓库 |

---

## 顺带说明：两个 compose 文件的分工

| 文件 | 镜像来源 | 什么时候用 |
|---|---|---|
| [`compose.yaml`](compose.yaml) | 本地 `docker build` 出来的 | 没走 GitHub，自己在 NAS 或电脑上构建 |
| [`compose.ghcr.yaml`](compose.ghcr.yaml) | `ghcr.io/...:latest` | 已配置 GitHub Actions，想一条命令升级 |
