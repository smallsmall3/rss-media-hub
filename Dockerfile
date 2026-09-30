# syntax=docker/dockerfile:1
# ============================================================================
#  RSS Media Hub — PT RSS 订阅器 + Emby/Jellyfin 入库比对 + Telegram 推送
#
#  多阶段构建：builder 只负责装依赖，运行层不带编译器与 pip 缓存。
#  入口脚本先用 root 修好挂载目录属主，再降权到 uid 1000 运行。
# ============================================================================

FROM python:3.12-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
 && /opt/venv/bin/pip install -r requirements.txt


FROM python:3.12-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="rss-media-hub" \
      org.opencontainers.image.description="PT RSS 订阅 + Emby/Jellyfin 入库比对 + Telegram 通知，追完自动退订" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    PATH="/opt/venv/bin:$PATH" \
    RMH_CONFIG_DIR=/config \
    RMH_STATE_DIR=/state \
    RMH_HEALTH_PORT=8080

RUN apt-get update \
 && apt-get install -y --no-install-recommends tini gosu tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd -g 1000 app \
 && useradd -u 1000 -g 1000 -m -s /usr/sbin/nologin app

COPY --from=builder /opt/venv /opt/venv
COPY app /app/app
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

WORKDIR /app
# sed 去掉可能的 CR：如果这个项目是在 Windows 上打包/解压的，entrypoint.sh 可能带
# CRLF 换行，那样 Linux 会把 "#!/bin/sh\r" 当成不存在的解释器，容器直接启动失败
# （表现为无限重启）。这里做一次兜底转换，同时修 .py 的换行。
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh \
 && chmod +x /usr/local/bin/entrypoint.sh \
 && mkdir -p /config /state /app \
 && chown -R app:app /app /config /state

VOLUME ["/config", "/state"]
EXPOSE 8080

# 构建信息：由 CI 通过 --build-arg 注入，界面左上角会显示
# 「版本号 · 提交」。这样 pull 之后一眼就能确认跑的是哪次构建，
# 不用去猜镜像 tag 有没有生效。
ARG RMH_BUILD_COMMIT=""
ARG RMH_BUILD_TIME=""
ARG RMH_BUILD_TAG=""
ENV RMH_BUILD_COMMIT=${RMH_BUILD_COMMIT} \
    RMH_BUILD_TIME=${RMH_BUILD_TIME} \
    RMH_BUILD_TAG=${RMH_BUILD_TAG}

# /healthz 由程序内置的极简 HTTP 服务提供，不需要额外装 curl
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('RMH_HEALTH_PORT','8080')+'/healthz',timeout=4)" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
# 容器以 root 起（为了修挂载目录属主），entrypoint 会自动 gosu 到 uid 1000
USER root
CMD ["python", "-m", "app", "run"]
