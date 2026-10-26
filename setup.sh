#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# QQ AI Bot —— 服务器一键初始化
#
# 适用：Alibaba Cloud Linux 3/4、Ubuntu 22.04+、Debian 12+、CentOS/Rocky 9
# 作用：加 swap → 装 Docker → 放行端口 → 建运行时目录
#
# 用法（在项目目录内，root 执行）：
#   cd /opt/qqbot && bash setup.sh
# ---------------------------------------------------------------------------
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
SWAP_SIZE="${SWAP_SIZE:-2G}"

info() { printf '\n\033[1;32m[+] %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }

if [ "$(id -u)" -ne 0 ]; then
    warn "需要 root 权限，请用：sudo bash setup.sh"
    exit 1
fi

. /etc/os-release
info "操作系统: ${PRETTY_NAME}"
info "项目目录: ${APP_DIR}"

# ---------------------------------------------------------------------------
# 1. swap —— 2G 机器处理视频/文档时的保命措施
# ---------------------------------------------------------------------------
if swapon --show 2>/dev/null | grep -q '/swapfile'; then
    info "swap 已存在，跳过"
else
    info "创建 ${SWAP_SIZE} 的 swap"
    if command -v fallocate >/dev/null 2>&1; then
        fallocate -l "$SWAP_SIZE" /swapfile
    else
        dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
    fi
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
    if ! grep -q '^/swapfile' /etc/fstab; then
        echo '/swapfile none swap sw 0 0' >> /etc/fstab
    fi
    info "swap 已启用，并写入 /etc/fstab（重启后依然有效）"
fi

# ---------------------------------------------------------------------------
# 2. Docker
#    Alibaba Cloud Linux / Anolis 的发行版 ID 是 alinux/anolis，
#    get.docker.com 官方脚本不认，会报 "Unsupported distribution 'alinux'"，
#    因此这两个系统改走阿里云 Docker CE 镜像源（EL9 兼容）。
# ---------------------------------------------------------------------------
install_docker_alinux() {
    info "检测到 ${ID}，改用阿里云 Docker CE 镜像源（EL9 兼容）"
    rpm --import https://mirrors.aliyun.com/docker-ce/linux/centos/gpg 2>/dev/null || true
    cat > /etc/yum.repos.d/docker-ce.repo <<'REPO'
[docker-ce-stable]
name=Docker CE Stable
baseurl=https://mirrors.aliyun.com/docker-ce/linux/centos/9/$basearch/stable
enabled=1
gpgcheck=0
REPO
    dnf makecache -y >/dev/null 2>&1 || true
    dnf install -y docker-ce docker-ce-cli containerd.io \
                   docker-buildx-plugin docker-compose-plugin
}

if command -v docker >/dev/null 2>&1; then
    info "Docker 已安装：$(docker --version)"
else
    case "${ID:-}" in
        alinux|anolis)
            install_docker_alinux
            ;;
        *)
            info "安装 Docker（官方脚本，可能需要 1-2 分钟）"
            curl -fsSL https://get.docker.com | sh
            ;;
    esac
fi
systemctl enable --now docker >/dev/null 2>&1 || true

if docker compose version >/dev/null 2>&1; then
    info "Compose 插件: $(docker compose version --short 2>/dev/null || echo ok)"
else
    warn "docker compose 插件不可用，尝试安装"
    if command -v dnf >/dev/null 2>&1; then
        dnf install -y docker-compose-plugin || warn "安装失败，请手动处理"
    else
        apt-get install -y docker-compose-plugin || warn "安装失败，请手动处理"
    fi
fi

# ---------------------------------------------------------------------------
# 3. SELinux / firewalld（RHEL 系专属坑）
# ---------------------------------------------------------------------------
if command -v getenforce >/dev/null 2>&1; then
    sel="$(getenforce 2>/dev/null || echo Unknown)"
    info "SELinux: ${sel}"
    if [ "$sel" = "Enforcing" ]; then
        warn "SELinux 为 Enforcing，容器 bind mount 可能被拒；为 Docker 放行容器相关布尔值"
        setsebool -P container_manage_cgroup on 2>/dev/null || true
    fi
fi

if systemctl is-active --quiet firewalld 2>/dev/null; then
    warn "firewalld 正在运行，放行 6099(WebUI) / 3001(OneBot WS)"
    firewall-cmd --permanent --add-port=6099/tcp >/dev/null 2>&1 || true
    firewall-cmd --permanent --add-port=3001/tcp >/dev/null 2>&1 || true
    firewall-cmd --reload >/dev/null 2>&1 || true
else
    info "firewalld 未运行（阿里云默认靠安全组控制）"
fi

# ---------------------------------------------------------------------------
# 4. 运行时目录与权限
# ---------------------------------------------------------------------------
cd "$APP_DIR"
mkdir -p cache downloads napcat/config napcat/qq
[ -f memory.json ] || { touch memory.json; info "已创建空的 memory.json"; }
if [ -f config.json ]; then
    chmod 600 config.json
    info "config.json 权限已收紧为 600（含明文 API key）"
else
    warn "config.json 不存在，请先上传后再启动"
fi

# ---------------------------------------------------------------------------
# 5. 结果汇总
# ---------------------------------------------------------------------------
info "内存与 swap"
free -h

info "下一步"
cat <<EOF
  cd ${APP_DIR}
  docker compose up -d --build     # 首次构建约 3-8 分钟
  docker compose logs -f bot       # 另开窗口看日志
EOF

warn "别忘了在阿里云控制台【安全组】放行 6099 端口，否则打不开 NapCat WebUI"
