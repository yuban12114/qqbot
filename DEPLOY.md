# 部署到云服务器（2 核 2G 版）

面向阿里云 / 腾讯云等 Linux 服务器，用 Docker Compose 同时跑 **NapCat（QQ 协议端）** 与 **本项目**。

> 目标规格：2 核 2G，系统盘 40G，带宽 ≥ 5Mbps。低于 2G 内存不建议。
>
> 系统假设：**Alibaba Cloud Linux 3/4**、Ubuntu 22.04+、Debian 12+。
> 注意 Alibaba Cloud Linux 4 属 **RHEL 系，包管理器是 `dnf` 而非 `apt`**——
> 本文的 Docker 方案完全不受影响（容器内是 Debian 环境），只有第 10 节裸机方案需按发行版选命令。

---

## ⚠️ 0. 前置：操作系统必须是 Linux

本文所有命令都基于 Linux。如果实例是 **Windows Server**，**不要往下做**，先去控制台更换操作系统。

三个硬性原因：

1. **内存**：Windows Server 2022/2025 空载即占用 1.2–1.5 GB，2G 机器只剩几百 MB，
   跑不动 QQ + Python + Chromium。Linux 空载仅约 200 MB。
2. **QQ 无法运行**：NapCat 依赖 QQ NT 客户端（Electron 图形程序）。阿里云的
   「不含图形化桌面」镜像是 Server Core，缺少桌面环境，QQ 装不上也起不来。
3. **Docker 不适用**：Docker Desktop 需要 WSL2/Hyper-V 与 GUI，Server Core 无法安装；
   只能用 Windows 容器，基础镜像体积以 GB 计，2G 内存无法构建。

### 更换操作（阿里云）

控制台 → 实例详情 → **更换操作系统** → 公共镜像选 `Ubuntu 24.04 64位` 或
`Alibaba Cloud Linux 3` → 设置 root 密码 → 确认（实例会重启，系统盘被重置）。

> 新购实例无数据，可放心更换。已付的 Windows 授权费用通常不退差价，
> 以控制台最终提示为准；建议在到期续费前先确认配置。

更换完成、能用 SSH 登录后，再从本文第 1 节开始。

---

## 0.5 整体结构

```
公网 ──► 6099 (NapCat WebUI，仅首次配置用)
         │
    ┌────┴─────────── docker network: botnet ───────────┐
    │                                                   │
 napcat (512m)  ◄── ws://napcat:3001 ──  bot (1200m)    │
    │                                        │          │
 ./napcat/qq (登录态)                  ./config.json     │
                                       ./memory.json     │
                                       ./cache, ./downloads
    └───────────────────────────────────────────────────┘
```

---

## 1. 服务器准备

### 1.1 安全组放行端口

| 端口 | 用途 | 是否长期开放 |
|------|------|-------------|
| 22 | SSH | 是（建议限制来源 IP） |
| 6099 | NapCat WebUI（扫码登录用） | **配置完成后关闭** |

不需要开放 3001（bot 走容器内网）。

### 1.2 加 2G swap（2 核 2G 必做）

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h    # 确认 Swap 一行有 2.0Gi
```

不做这步，视频/文档处理时容易触发 OOM Killer 直接杀掉进程。

### 1.3 安装 Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo systemctl enable --now docker
docker compose version     # 需要 v2.x
```

国内拉镜像慢可配置加速器（可选）：

```bash
sudo mkdir -p /etc/docker
sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{ "registry-mirrors": ["https://docker.mirrors.ustc.edu.cn"] }
EOF
sudo systemctl restart docker
```

### 1.4 一键完成 1.2 / 1.3 / 第 3 节（推荐）

项目自带 `setup.sh`，自动完成：加 swap → 装 Docker → 放行 firewalld 端口 →
SELinux 兼容处理 → 建运行时目录 → 收紧 config.json 权限。

```bash
cd /opt/qqbot          # 解包后进入项目目录
sudo bash setup.sh
```

脚本可重复执行（已完成的步骤会跳过）。若想手工逐步操作，继续看 1.2 / 1.3 的命令。

> 若执行时报 `$'\r': command not found`，说明文件在 Windows 上被转成了 CRLF 换行，
> 先修正：`sed -i 's/\r$//' setup.sh` 再执行。

---

## 2. 本地准备：改 3 处配置

编辑 `config.json`，**这三处必须改**：

### ① 连接地址改成容器服务名

```json
"onebot": {
  "ws_url": "ws://napcat:3001",     // 原为 ws://127.0.0.1:3001
  "token": "b5jCOpRNJ1rv4j.F",       // 记住这个值，NapCat 那边要填一样的
  "bot_qq": ""                        // 建议填机器人 QQ 号，@ 判断更准
}
```

### ② 下载体积上限（按内存规格选择）

```json
"web": {
  "media_download_max_seconds": 3600,
  "media_download_max_mb": 500
}
```

**两个上限，取小者生效：**

| 限制来源 | 上限 | 说明 |
|---------|------|------|
| **方舟 Files API**（默认存储空间） | **512 MB** | 硬限制，超过会返回 HTTP 400，本地配再大也没用 |
| 本机内存 | 4G 内存可到 500；2G 建议 100–250 | 流式上传后峰值≈文件大小×1，不再×3 |
| 磁盘 | 40G 系统盘足够 | 下载的视频落在 `downloads/` 与临时目录 |

> 想突破 512MB 需要把文件先传到**火山引擎对象存储 TOS Bucket**（上限 2GB），
> 或改用 AI MediaKit（URL 方式上限 5GB、时长 2 小时），都需要额外的鉴权与配置。

500MB 对应的视频长度（按 480p 码率估算）：

| 码率 | 可容纳时长 |
|------|-----------|
| 500 kbps（低码率） | 约 137 分钟 |
| 800 kbps（常规） | 约 85 分钟 |
| 1500 kbps（720p） | 约 46 分钟 |

本项目的 `download_media` 固定下载 480p 以内，所以实际多为 85–137 分钟区间，
配合 `media_download_max_seconds: 3600`（60 分钟）是稳妥组合。

> **2026-10-24 起**，`Files API` 上传改为**流式发送**（`_post_multipart_stream`），
> 文件按 256KB 分块发送，不再把整个请求体拼进内存。因此大视频的内存峰值
> 从「文件大小 × 3」降到「文件大小 × 1 + 256KB」。
> 但 Base64 回退路径仍有 3 倍放大，所以代码对 **>200MB 的文件禁止回退 Base64**，
> 上传失败时直接报错而不是 OOM。

### ③ 确认缓存目录是相对路径

```json
"services": { "openai": { "cache_dir": "cache" } },
"memory": { "persistence": { "path": "memory.json" } }
```

保持相对路径，才能被 compose 的挂载接住。

---

## 3. 上传代码

在本地 Git Bash 执行（**注意排除 venv**，Windows 的 venv 在 Linux 上不可用）：

```bash
cd /e/WorkBuddy_document/2026-10-21-20-36-55

tar --exclude='qq-ai-reply/venv' \
    --exclude='qq-ai-reply/__pycache__' \
    --exclude='qq-ai-reply/cache' \
    --exclude='qq-ai-reply/downloads' \
    --exclude='qq-ai-reply/napcat' \
    -czf qqbot.tar.gz qq-ai-reply

scp qqbot.tar.gz root@<服务器IP>:/opt/
```

服务器上解包：

```bash
cd /opt
sudo tar -xzf qqbot.tar.gz
sudo mv qq-ai-reply qqbot
cd /opt/qqbot

# 创建运行时目录（Docker 挂载文件前必须先存在，否则会被建成目录）
sudo mkdir -p cache downloads napcat/config napcat/qq
sudo touch memory.json
sudo chmod 600 config.json        # 含明文 API key
```

> **关于 memory.json**：tar 包里包含你本地的对话记忆（14KB）。想延续记忆就保留；
> 想从零开始就先 `rm memory.json && touch memory.json`——**不能只删不建**，
> 否则 Docker 会把挂载点建成目录，bot 启动时报错。

> **关于上传体积**：`venv/` 必须排除（Windows 的虚拟环境在 Linux 上完全不可用，
> 且占 237MB）。tar 包正常应在 1MB 以内（主要是代码 + 一份 config.json）。

---

## 4. 启动

```bash
cd /opt/qqbot
sudo docker compose up -d --build
```

首次构建约 3–8 分钟（要下载 ~400MB 的 Chromium 及系统库）。构建期间内存峰值约 600MB，2G 机器可承受。

查看状态：

```bash
sudo docker compose ps
sudo docker compose logs -f bot
```

此时 bot 会反复重连失败——正常，NapCat 还没登录。

---

## 5. NapCat 登录与 WS 配置

### 5.1 取 WebUI token

```bash
sudo docker compose logs napcat | grep -i token
```

日志里会打印形如 `WebUI Token: xxxxxxxx`。

### 5.2 扫码登录

浏览器打开 `http://<服务器IP>:6099`，输入 token，按提示用手机 QQ 扫码。

登录成功后，登录态保存在 `./napcat/qq`，**后续重启不需要重新扫码**。

> NapCat 不同版本的持久化路径可能调整。若重启后仍需扫码，用
> `docker compose exec napcat ls /app/napcat/config` 确认实际目录并修正
> `docker-compose.yml` 的 volumes 映射。

### 5.3 配置 OneBot WebSocket 服务端

在 WebUI 的「网络配置」中新增 **WebSocket 服务端**：

| 字段 | 值 | 说明 |
|------|-----|------|
| Host | `0.0.0.0` | **填 127.0.0.1 会导致 bot 连不上**（不同容器） |
| Port | `3001` | 与 `ws_url` 一致 |
| Token | `b5jCOpRNJ1rv4j.F` | 必须与 `config.json` 的 `onebot.token` 完全一致 |
| 消息格式 | `array` | 本项目按消息段数组解析 |

保存后 NapCat 会重载配置。

### 5.4 验证连通

```bash
sudo docker compose logs -f bot
```

成功时会看到 WebSocket 连接建立、`get_login_info` 返回机器人昵称。然后私聊机器人发一条消息测试。

---

## 6. 收尾：关闭 WebUI 公网暴露

登录成功后，把 compose 里的端口映射改掉，避免 6099 暴露在公网：

```yaml
    ports:
      - "127.0.0.1:6099:6099"    # 改成只绑本机，需要时用 SSH 隧道访问
```

```bash
sudo docker compose up -d napcat
```

之后需要再进 WebUI 时，用 SSH 隧道：

```bash
ssh -L 6099:127.0.0.1:6099 root@<服务器IP>
# 本地浏览器访问 http://127.0.0.1:6099
```

---

## 7. 日常运维

```bash
# 看日志（bot）
docker compose logs -f --tail=200 bot

# 重启
docker compose restart bot

# 更新代码后重建
tar -xzf qqbot.tar.gz -C /tmp && rsync -a /tmp/qq-ai-reply/ /opt/qqbot/ --exclude config.json
cd /opt/qqbot && docker compose up -d --build bot

# 备份（配置 + 记忆 + 登录态）
tar -czf backup-$(date +%F).tar.gz config.json memory.json napcat/
```

### 内存巡检（2G 机器建议每天看一眼）

```bash
free -h
docker stats --no-stream
# 有没有被 OOM 杀过
sudo dmesg | grep -i "killed process"
```

如果 `dmesg` 里出现 `Killed process ... (python)`，说明内存不足，按第 8 节处理。

---

## 8. 2 核 2G 的调优清单

| 措施 | 位置 | 作用 |
|------|------|------|
| 2G swap | 系统 | 峰值时保命，避免被杀 |
| `media_download_max_mb` | config.json | 2G 用 100–250；4G 可用 1000（已有流式上传） |
| `media_download_max_seconds` | config.json | 配合体积上限，避免下载超长视频 |
| `media_max_videos: 1` | config.json | 一条消息最多分析 1 个视频 |
| `page_media: true`（保留） | config.json | 关掉会失去网页媒体能力；若内存吃紧可先设 false |
| `mem_limit: 1200m` | compose | 防止 bot 挤占 NapCat 导致双双失联；2G 机器建议 1200m，4G 可提到 2600m |
| `PYTHONUNBUFFERED=1` | Dockerfile | 日志实时输出，便于排查 |

**能力与成本的取舍**：2G 下建议把视频分析当"偶尔用"，日常以文字/图片/小文档为主。如果重度使用视频，升到 4G 更划算。

### 带宽成本（容易忽略）

阿里云常见售卖方式是「100 Mbps 峰值 + 按使用流量计费」，单价约 ¥0.8/GB（中国大陆）。
本项目的流量消耗点：

| 行为 | 单次流量 |
|------|---------|
| 分析一个 B 站 200MB 视频 | 200 MB（下载）+ 少量上传 |
| 读一篇 arXiv 论文 PDF | 3–10 MB |
| 抓一个普通网页 | 0.2–2 MB |
| QQ 收到的图片/文件 | 原大小 |

按每天分析 10 个 100MB 视频估算 ≈ 1 GB/天 ≈ **¥24/月**，且会随使用量线性增长。

若用量偏大，建议改成**固定带宽**（5–10 Mbps 包月），成本可预期；否则就用
`media_download_max_mb` 控制单次下载体积。

---

## 9. 常见问题

**Q1：bot 日志一直重连 `ws://napcat:3001`**
按顺序排查：
1. NapCat 是否登录成功（`docker compose logs napcat` 有报错吗）
2. WS 服务端 Host 是否 `0.0.0.0`（填 127.0.0.1 必失败）
3. Token 是否与 `config.json` 完全一致（含大小写）
4. `docker compose exec bot python -c "import socket;socket.create_connection(('napcat',3001),5);print('ok')"` 验证网络层是否通

**Q2：`docker compose up` 时 bot 反复重启**
```bash
docker compose logs --tail=100 bot
```
常见原因：`config.json` 挂载失败（文件不存在会被建成目录）、JSON 语法错误、`memory.json` 不存在导致写入失败。

**Q3：处理视频后 bot 掉线，日志无 traceback**
就是 OOM。检查 `sudo dmesg | grep -i "killed process"`，然后调小 `media_download_max_mb`、确认 swap 已启用。

**Q4：Playwright 报 Chromium 启动失败**
本项目已传 `--no-sandbox`。若仍失败，确认镜像构建时 `playwright install --with-deps chromium` 成功执行（看构建日志有无报错）。

**Q5：构建镜像时被中断**
多半是内存不足。确认 swap 已开；或在本地构建后推到阿里云 ACR，服务器只 `docker pull`。

**Q6：QQ 账号被要求验证 / 冻结**
机房 IP 登录 QQ 的固有风险，与代码无关。建议先用小号试跑，且不要在服务器上频繁切换登录设备。

**Q7：打不开 `http://IP:6099`（NapCat WebUI）**

按顺序排查：
1. **阿里云安全组**是否放行 6099（这是最常见原因，控制台 → 安全组 → 入方向）
2. 服务器本地防火墙：`sudo firewall-cmd --list-ports`（Alibaba Cloud Linux 默认可能启用 firewalld），
   没有 6099 则执行 `sudo firewall-cmd --permanent --add-port=6099/tcp && sudo firewall-cmd --reload`
3. 容器是否在跑：`docker compose ps`

**Q8：Alibaba Cloud Linux 上挂载目录权限报错（SELinux）**

先看状态：`getenforce`。若是 `Enforcing`，为容器加标签或放行布尔值：

```bash
sudo setsebool -P container_manage_cgroup on
# 或给挂载目录打上容器可读写标签
sudo chcon -Rt container_file_t /opt/qqbot/{cache,downloads,napcat}
```

`setup.sh` 已处理第一项。若仍报 `Permission denied`，可用 `sudo setenforce 0` 临时验证是否为
SELinux 引起（确认后再决定是否永久调整，不建议生产环境长期关闭）。

**Q9：构建时卡在 `[2/7] RUN apt-get update && apt-get install ffmpeg` 很久**

Debian 官方源在境外，ffmpeg 有 100+ 依赖包，国内直连可能超过 10 分钟。
当前 `Dockerfile` 已把 apt 与 pip 都换成阿里云镜像源，正常应在 1–2 分钟内完成。
若服务器上跑的是旧版 Dockerfile，重新上传后重建：

```powershell
scp E:\WorkBuddy_document\2026-10-21-20-36-55\qq-ai-reply\Dockerfile root@<IP>:/opt/qqbot/
```

```bash
cd /opt/qqbot && docker compose up -d --build
```

**Q10：构建卡在 `playwright install chromium` 下载内核**

Chromium 内核走 Playwright 官方 CDN（境外）。若长时间无进度，可加国内镜像重试：

```bash
# 在 Dockerfile 的 FROM 之后插入一行
ENV PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright
```

该地址为社区镜像，若失效可改回默认（删除该行即可），或改从其他可用镜像源获取。

**Q11：B 站链接报 `HTTP Error 412`**

412 是 B 站的反爬风控，**不是链接失效**。云服务器（机房 IP）访问 B 站接口被标记的概率很高，
同一段代码在家宽上正常、在服务器上 412，就是这个原因。

解决：给 bot 配上你自己的 B 站 Cookie。

1. 浏览器登录 [bilibili.com](https://www.bilibili.com)
2. 按 `F12` 打开开发者工具 → 「网络 / Network」标签 → 刷新页面
3. 点任意一个请求 → 右侧「请求标头 / Request Headers」→ 找到 `Cookie:` 那一行
4. **复制整行的值**（`SESSDATA=...; buvid3=...; b_nut=...` 这一长串）
5. 填进 `config.json`：

```json
"web": {
  "bilibili_cookie": "SESSDATA=xxxx; buvid3=yyyy; b_nut=zzzz"
}
```

6. 重启 bot（不影响 NapCat 登录）：

```bash
docker compose restart bot
```

> `SESSDATA` 是登录凭据（有效期约数月），等同于你的 B 站登录态，**不要外传**。
> 配置后 `_fetch_bilibili`、`_resolve_bilibili_bvid` 与 yt-dlp 下载都会带上它。
> 若带 Cookie 仍是 412，说明该出口 IP 被深度标记，需换 IP 或改用其他方式取视频。

**Q12：日志出现 `保存记忆文件失败：[Errno 16] Device or resource busy: '/app/memory.json.tmp' -> '/app/memory.json'`**

这是 Docker **单文件 bind mount** 的固有行为：`./memory.json:/app/memory.json` 让该路径成为挂载点，
而挂载点**不能被 rename 覆盖**，所以"写临时文件再原子替换"会返回 `EBUSY`。

影响：对话上下文（记忆）存不下来，重启后 bot 会忘记之前聊过什么；收发消息本身正常。

代码已做兼容（`main.py:_save_memory`）：原子替换失败时自动回退为
「`copyfile` 覆盖同一 inode 的内容」，内容不丢，只牺牲原子性。
更新 `main.py` 并执行 `docker compose up -d --build bot` 即可消除该告警。

**更规范的长期做法**是把「文件挂载」改成「目录挂载」（挂载点变成目录，rename 就不受限）：

```bash
cd /opt/qqbot
mkdir -p data
[ -f memory.json ] && mv memory.json data/memory.json
mkdir -p cache downloads        # 保持原有目录挂载不变
```

`docker-compose.yml` 里把单文件挂载替换成目录：

```yaml
    volumes:
      - ./data:/app/data
      # 删除这行：- ./memory.json:/app/memory.json
```

`config.json` 里把路径改到新目录：

```json
"memory": { "persistence": { "path": "data/memory.json" } }
```

> 容器内路径随挂载点变化（`/app/data/memory.json`），配置文件与挂载必须一致。

**Q13：重启 NapCat 每次都要重新扫码，能免扫吗？**

可以，但**首次登录和登录态失效时仍然必须扫码**，免扫只对「已有有效登录态」的重启生效。

两种版本参数不同，不要混用：

| 部署方式 | 快速登录参数 |
|---------|------------|
| Shell / AppImage（`napcat.sh`、`NapCat.AppImage`） | `-q 123456789` |
| **Docker（本项目）** | **环境变量 `ACCOUNT=123456789`** |

`docker-compose.yml` 里已经加上：

```yaml
    environment:
      - ACCOUNT=2024514917
```

前提条件（缺一不可）：

1. **登录态必须持久化** —— `./napcat/qq:/app/.config/QQ` 已配好；这个目录为空时无从"快速"登录。
2. **配置文件存在** —— `./napcat/config/napcat_<QQ>.json`。entrypoint 在没设 `ACCOUNT` 时会从这个文件名自动识别 QQ 号。
3. 登录态未过期、未被腾讯判定为异常设备。

生效方式：改完 compose 后 `docker compose up -d`（会重建 napcat 容器）。若日志直接显示已登录、没有二维码，说明快速登录成功。

排查：`docker compose logs napcat | grep -i -E "qrcode|二维码|quick|快速"`

---

## 10. 附：不用 Docker 的裸机方案（更省内存）

若内存实在吃紧，bot 走裸机、NapCat 仍用 Docker，可省约 100–200MB。

```bash
# 系统依赖（按发行版二选一）
# Ubuntu / Debian：
sudo apt update && sudo apt install -y python3-venv python3-pip ffmpeg
# Alibaba Cloud Linux 4 / RHEL 系：
sudo dnf install -y python3 python3-pip
sudo dnf install -y ffmpeg || sudo dnf install -y --enablerepo=epel ffmpeg
# ↑ 若 dnf 找不到 ffmpeg，说明默认源未包含，需先启用 EPEL：
#   sudo dnf install -y epel-release

# 建 venv 并装依赖
cd /opt/qqbot
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/playwright install --with-deps chromium

# systemd
sudo tee /etc/systemd/system/qqbot.service >/dev/null <<'EOF'
[Unit]
Description=QQ AI Reply Bot
After=network-online.target docker.service

[Service]
Type=simple
WorkingDirectory=/opt/qqbot
ExecStart=/opt/qqbot/venv/bin/python -u main.py
Restart=always
RestartSec=5
Environment=TZ=Asia/Shanghai

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now qqbot
sudo journalctl -u qqbot -f
```

此时 `ws_url` 填 `ws://127.0.0.1:3001`（NapCat 容器的 3001 已映射到宿主机）。
