# QQ AI 自动回复 Bot —— 生产镜像
# 目标环境：Linux / 2核2G 云服务器
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Shanghai

# 换国内镜像源再用 apt：Debian 官方源在境外，装 ffmpeg（100+ 依赖包）
# 在国内服务器上可能耗时 10 分钟以上；阿里云镜像通常 1-2 分钟。
# 兼容 deb822（debian.sources）与传统 sources.list 两种格式。
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
        sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list; \
    fi; \
    apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        curl \
        tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖，利用 Docker 层缓存（代码改动不会重装依赖）
# 同样换阿里云 PyPI 镜像，避免 pip 从境外拉包
COPY requirements.txt ./
RUN pip install --no-cache-dir \
        -i https://mirrors.aliyun.com/pypi/simple/ \
        --trusted-host mirrors.aliyun.com \
        -r requirements.txt

# Playwright 的 Chromium 内核 + 它需要的系统库（约 400MB）
RUN playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# 业务代码（config.json 通过 compose 挂载，不进镜像，避免 API key 泄漏）
COPY . .

CMD ["python", "-u", "main.py"]
