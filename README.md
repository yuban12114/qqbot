# 鱼小板 · QQ AI 多模态自动回复 Bot

> 项目说明：用 NapCat 搭建的 QQ 机器人。LLM 接 DeepSeek，图片 / 文档 / 音频的生成与识别使用火山引擎 API；其他模型未做验证。

基于 **OneBot v11（NapCat）** 的个人 QQ 机器人。接入一个真实 QQ 号，自动处理私聊与群聊消息：
理解图片 / 视频 / 音频 / 文档，联网搜索，生成图片，合成语音，并带持久化多轮记忆与媒体追问能力。

> 个人自用与技术研究项目，依赖非官方协议端，**存在账号风控风险**。详见文末「风险与合规」。

---

## 功能一览

| 能力 | 说明 | 依赖 |
|---|---|---|
| 文本对话 | 私聊 / 群聊（可配置仅 @ 触发），保留多轮上下文 | DeepSeek（OpenAI 兼容） |
| 图片理解 | 识别内容、读图中文字、按用户提问定向回答 | 方舟 `doubao-seed-2-1-lite` |
| 视频理解 | 抽帧 + **独立音轨**一起送模型（画面与声音都能理解） | 方舟同模型 + ffmpeg |
| 音频理解 | 语音消息直接听；ogg/opus 自动转 mp3 | 方舟同模型 + ffmpeg |
| 文档理解 | PDF / Word / Excel / PPT / 纯文本 | 方舟 Files API |
| 文生图 | 按描述生成图片并作为图片消息发出 | 方舟 `doubao-seedream` |
| 语音合成 | 文字转语音消息 | 豆包 openspeech（需开通） |
| 语音识别 | 语音转文字（音频理解失败时的兜底） | 豆包 openspeech（需开通） |
| 联网搜索 | 实时信息查询，DeepSeek 官方搜索优先，四级引擎兜底 | DeepSeek + 公网 |
| 网页抓取 | 普通页面 / SPA 动态渲染 / 页面内嵌视频与图片 | Playwright + yt-dlp |
| 媒体追问 | 对发过的媒体重新提问（`m1` 编号 + 工具调用），三级降级复用 | 方舟 Files API |
| 引用 / 合并转发 | 解析被引用消息、合并转发的聊天记录（含嵌套） | NapCat |
| 文件消息 | 私聊文件、群文件上传通知、引用文件 | NapCat |

---

## 架构

```
QQ 客户端
   ↕  (QQ 协议)
NapCat（协议端，登录真实 QQ 号）
   ↕  OneBot v11 正向 WebSocket
main.py            传输层：连接、事件队列、消息段解析、结构化发送
   ↕
agent.py           编排层：输入归一化 → 工具循环 → 记忆 → 输出拼装
   ↙        ↘
ai_client.py   services.py
(LLM 大脑)      (多模态「手」：视觉/视频/音频/文档/文生图/TTS/ASR/搜索/网页)
```

设计要点：

- **事件循环不阻塞**：`main.py` 的读循环只负责收取事件与 echo，业务处理进入 `asyncio.Queue`，由单独 worker 串行消费。任何一条耗时消息都不会拖慢后续事件的接收。
- **阻塞调用异步化**：搜索引擎抓取、网页下载等同步 IO 统一走 `asyncio.to_thread`，避免卡住事件循环。
- **媒体只存指针**：会话记忆中的媒体索引只保存 `file_id` / `url` / `msg_id` 等指针，不保存描述与本地路径，控制记忆体积。
- **三级降级**：追问时依次尝试「直接复用 file_id」→「GET 校验」→「调 NapCat `get_msg` 重新下载」，任一可用即返回。

---

## 目录结构

```
qq-ai-reply/
├── main.py                 OneBot v11 WebSocket 客户端（传输层）
├── agent.py                编排层：工具循环、记忆管理、回复拼装
├── ai_client.py            LLM HTTP 客户端（DeepSeek / 任意 OpenAI 兼容端点）
├── services.py             多模态服务：方舟 Ark + openspeech + 搜索 + 网页抓取
├── media.py                NapCat 媒体下载与本地落盘
├── config.py               配置加载：默认值深合并 + 环境变量覆盖
├── config.json             实际配置（含明文密钥，已被 .gitignore 排除）
├── maintain.py             持久化数据查看 / 选择性清理 / 初始化
├── cleanup_remote.py       方舟 Files API 远端文件用量查询与清理
├── cleanup_remote.bat      Windows 双击包装
├── Dockerfile              生产镜像（Linux，含 ffmpeg + Chromium）
├── docker-compose.yml      napcat + bot 双容器编排
├── setup.sh                服务器一键初始化（swap / Docker / 目录）
├── DEPLOY.md               云服务器部署详解与排错
├── requirements.txt        Python 依赖
├── .gitignore / .dockerignore
├── test_features.py        功能测试（18 项：媒体、记忆、搜索等）
├── test_video.py           视频 / 网页 / 媒体链路测试（43 项）
├── test_connectivity.py    一次性连通性探测（LLM + 各服务）
├── verify_models_e2e.py    一次性模型端到端验证（音频 / 文档）
├── memory.json             运行时对话记忆（自动生成，可删）
├── cache/                  上传临时文件、生成图、TTS 输出
├── downloads/              QQ 媒体落盘目录
└── venv/                   本地虚拟环境（不提交）
```

---

## 快速开始

### 0. 前置条件

| 项目 | 要求 |
|---|---|
| Python | 3.12+（开发环境为 3.14） |
| ffmpeg | 必须在 PATH 中；用于视频抽音轨、音频转码 |
| NapCat | 一个已登录的 QQ 号，开启 **OneBot v11 正向 WebSocket** |
| API Key | DeepSeek（大脑）+ 火山方舟 Ark（多模态），语音另需 openspeech Key |
| 可选 | Playwright Chromium（仅网页渲染 / 网页媒体需要） |

### 1. 本地运行（Windows）

```powershell
cd qq-ai-reply

# 1) 建虚拟环境并安装依赖
python -m venv venv
venv\Scripts\pip install -r requirements.txt

# 2) 可选：网页渲染需要浏览器内核（约 300MB）
venv\Scripts\playwright install chromium

# 3) 配置
#    onebot.ws_url  -> ws://127.0.0.1:3001
#    onebot.token   -> 与 NapCat 中设置的一致
#    也可用环境变量临时覆盖，不改文件：
$env:NAPCAT_WS_URL = "ws://127.0.0.1:3001"

# 4) 启动
venv\Scripts\python main.py
```

启动后日志出现 WebSocket 连接成功即可。QQ 上私聊机器人，或在群里 @ 它。

### 2. 服务器部署（Docker）

完整步骤见 [`DEPLOY.md`](DEPLOY.md)，最小流程：

```bash
cd /opt/qqbot
bash setup.sh                  # 加 swap、装 Docker、建运行时目录
docker compose up -d --build   # 构建并启动 napcat + bot
docker compose logs -f bot
```

两处关键差异：

- 容器内 `onebot.ws_url` 必须是 `ws://napcat:3001`（服务名，不是 127.0.0.1）。
- NapCat 容器首次启动需在 WebUI（`http://<IP>:6099`）扫码登录，之后登录态保存在 `napcat/qq`。

---

## 配置说明（`config.json`）

配置文件与 `config.py` 中的 `DEFAULTS` 做**深合并**：只写需要改的字段即可，缺省项自动取默认值。
另支持环境变量 `NAPCAT_WS_URL` 覆盖 `onebot.ws_url`，便于同一份配置在服务器与本地之间切换。

### 1. `onebot` —— 连接协议端

| 字段 | 当前值 | 说明 |
|---|---|---|
| `ws_url` | `ws://napcat:3001` | NapCat 的 OneBot v11 正向 WS 地址。Docker 用服务名；本机改 `ws://127.0.0.1:3001` |
| `token` | `b5jC...4j.F` | WS 鉴权 token，必须与 NapCat 配置**完全一致**（注意首尾空格 / Tab） |
| `bot_qq` | `""` | 机器人 QQ 号，留空则由 NapCat 自动上报 |

### 2. `openai` —— 大脑（LLM）

| 字段 | 当前值 | 说明 |
|---|---|---|
| `api_key` | `sk-...` | DeepSeek API Key |
| `base_url` | `https://api.deepseek.com` | OpenAI 兼容端点，可换成任意兼容服务 |
| `model` | `deepseek-flash` | 对话模型 |
| `temperature` | `0.8` | 随机性，越高越发散 |
| `max_tokens` | `4000` | 单次输出 token 上限（正文 + 工具参数共用），与 `reply_rules.max_reply_length` 取同一个数量级 |
| `request_timeout` | `600` | 单次请求超时（秒），长思考模型需要放宽 |
| `system_prompt` | `【PERSONA_LOAD】...` | 人设提示词。当前为结构化标记串，可整体替换为任意自然语言人设 |

### 3. `reply_rules` —— 回复策略

| 字段 | 当前值 | 说明 |
|---|---|---|
| `enable_private` | `true` | 是否回复私聊 |
| `enable_group` | `true` | 是否回复群聊 |
| `group_at_only` | `true` | 群聊仅在被 @ 时响应，避免刷屏 |
| `friend_whitelist` | `[]` | 私聊白名单（QQ 号数组），空 = 不限制 |
| `group_whitelist` | `[]` | 群白名单（群号数组），空 = 不限制 |
| `ignore_self` | `true` | 忽略机器人自己发出的消息，防自问自答 |
| `cooldown_seconds` | `1.5` | 同一会话最小回复间隔，防风控 |
| `max_reply_length` | `4000` | 单条回复字符上限，超出硬截断（不发省略号），建议与 `max_tokens` 同步调整 |
| `group_sender_prefix` | `true` | 群聊共享上下文时给消息加「昵称：」前缀，便于模型区分发言人 |
| `reply_to_files` | `false` | 是否自动响应群文件 / 离线文件通知。关闭后引用文件仍可分析 |
| `separate_media_replies` | 默认 `true` | 文字 / 图片 / 语音是否拆成独立消息发送（未写入本配置，取默认值） |
| `media_send_interval` | 默认 `0.3` | 分离发送时相邻媒体消息间隔（秒），规避「发送过快」 |

### 4. `memory` —— 会话记忆

| 字段 | 当前值 | 说明 |
|---|---|---|
| `enable` | `true` | 是否启用多轮记忆 |
| `mode` | `accumulate` | `accumulate` = 全量累积 + 超阈值摘要；`sliding` = 仅保留最近 N 轮 |
| `max_history` | `12` | 仅 `sliding` 模式使用 |
| `summarize_threshold_tokens` | `800000` | 估算输入超过该值**且**原文条数超过 `keep_recent_turns × 2` 时才摘要。当前值实际等于「几乎不压缩」 |
| `keep_recent_turns` | `200` | 摘要后保留的最近原文轮数 |
| `enable_cache_log` | `true` | 打印 DeepSeek 上下文缓存命中 / 未命中 token 数 |
| `persistence.enable` | `true` | 是否把记忆持久化到磁盘 |
| `persistence.path` | `memory.json` | 记忆文件路径（相对项目根） |
| `persistence.save_interval_seconds` | `120` | 定时落盘间隔；进程退出时也会保存 |

> `summarize_threshold_tokens` 设得过高会让 `recent` 只增不减。DeepSeek 上下文为 128K token，
> 长期高频使用最终会撞上限。建议定期用 `maintain.py` 清理，或把该值调到合理区间。

### 5. `services.openai` —— 多模态「手」（方舟 Ark）

| 字段 | 当前值 | 说明 |
|---|---|---|
| `api_key` | `ark-...` | 火山方舟 API Key；留空则全部多模态能力降级，仅剩纯文本 |
| `base_url` | `https://ark.cn-beijing.volces.com/api/v3` | 方舟 OpenAI 兼容端点 |
| `vision_model` | `doubao-seed-2-1-lite-260915` | 图片理解 |
| `video_model` | 同上 | 视频理解（同模型支持四类输入，视频更快） |
| `audio_model` | 同上 | 音频理解（`input_audio`，支持 `file_id`） |
| `document_model` | 同上 | 文档理解（`file` 内容类型，按页拆图） |
| `image_model` | `doubao-seedream-5-0-flash-260915` | 文生图 |
| `video_fps` | `0.3` | 视频抽帧率，越高越细但越慢越贵 |
| `video_with_audio` | `true` | 是否把音轨作为独立 `input_audio` 一起送；关掉会「听不见视频声音」 |
| `files_retention` | `keep` | `keep` = 不主动删（依赖方舟 7～30 天过期）；`immediate` = 用完即删 |
| `media_expire_days` | `30` | 上传时设置 `expire_at`（1～30 天，0 = 方舟默认 7 天），拉长以便追问复用 `file_id` |
| `tts_model` | `seed-audio-1.0` | **仅信息字段**，实际模型由 `tts_url` + `tts_resource_id` 决定 |
| `asr_model` | `doubao-seed-asr-2.0` | **仅信息字段**，实际由 `asr_url` + `asr_resource_id` 决定 |
| `tts_voice` | `S_p6aKYAXi2` | 音色 ID |
| `image_size` | `2K` | 生成图分辨率 |
| `image_watermark` | `false` | 是否加「AI 生成」水印 |
| `cache_dir` | `cache` | 生成图 / 语音 / 上传临时文件目录 |
| `voice_api_key` | `...` | **openspeech 专用 Key**。不接受方舟 Key，否则 401 |
| `tts_url` | `.../api/v3/tts/create` | 语音合成端点（`X-Api-Key` + `X-Api-Request-Id`） |
| `tts_resource_id` | `volc.seedtts.default` | TTS 资源号（create 接口未使用，保留兼容） |
| `asr_url` | `.../api/v3/auc/bigmodel/submit` | 语音识别提交端点，另有 query 轮询 |
| `asr_resource_id` | `volc.seedasr.auc` | ASR 资源号 |
| `asr_language` | `zh-CN` | 识别语言 |

**远端文件配额**：方舟单文件上限 512 MB（视频走 TOS 可达 2 GB），总容量 20 GB。
`keep` 模式下文件到期自动回收，正常使用无需频繁清理。

### 6. `web` —— 网页抓取与搜索

| 字段 | 当前值 | 说明 |
|---|---|---|
| `headless_browser` | `true` | 启用 Playwright 渲染；关闭后 SPA 站点抓不到正文 |
| `spa_text_threshold` | `200` | 静态剥离后文字少于该值且是 HTML → 判定疑似 SPA，改用浏览器 |
| `browser_timeout` | `20` | 浏览器渲染超时（秒） |
| `wait_until` | `load` | 等待策略：`load` / `domcontentloaded` / `networkidle` |
| `render_wait_ms` | `1500` | `load` 之后额外等待，让前端数据渲染完成 |
| `page_media` | `true` | 是否分析页面内嵌视频 / 图片 / 文档 |
| `media_max_videos` | `1` | 每页最多分析几个视频（平台播放器需下载，耗时长） |
| `media_max_images` | `1` | 每页最多分析几张图片 |
| `media_max_docs` | `1` | 每页最多分析几个文档链接 |
| `probe_content_type` | `true` | 链接无扩展名时用 HEAD 探测类型，识别下载型直链 |
| `deepseek_search.enable` | `true` | 启用 DeepSeek 官方搜索（`/v1/tools/search`）；失败自动回退原引擎链 |
| `deepseek_search.count` | `5` | 返回条数（1～50） |
| `deepseek_search.need_content` | `false` | 是否连正文一起返回（更全但更慢更贵） |
| `deepseek_search.timeout` | `30` | 单次搜索超时（秒） |
| `bilibili_cookie` | `""` | 可选。机房 IP 访问 B 站常被风控 412，填 `SESSDATA=xxx` 可缓解 |
| `media_download_max_seconds` | `3600` | 平台视频下载时长上限（秒）；当前值几乎不限制 |
| `media_download_max_mb` | `500` | 下载体积上限（MB）。需 ≤ 方舟单文件限制，否则上传会 400 |

搜索降级链：**DeepSeek 官方接口 → Bing → DuckDuckGo → 百度 → 搜狗**。后四级始终保留作保底。

### 7. `timeouts` —— 各环节超时

集中管理「业务级」超时。`config.json` 是 bind mount，改完 `docker compose restart bot` 即可生效，不必重建镜像。

| 字段 | 当前值 | 作用范围 |
|---|---|---|
| `onebot_api` | `10` | 单次 OneBot API 调用（`get_msg` / `send_msg` / `get_group_file_url` 等）的默认等待；个别调用点可显式传更短值（登录信息 5 秒） |
| `event_handle` | `1200` | **一条消息从下载 → 理解 → 回复的总上限**。到点取消这一条、给对方回一句「处理超时」，然后继续处理下一条。它是全局串行队列下「其他人最多等多久」的上界，别随手调大 |
| `file_upload` | `240` | 上传单个文件到方舟 Files API（100MB 在 3.5Mbps 上行下约 240 秒） |
| `video_understand` | `240` | 单次视频理解请求 |
| `ffmpeg` | `120` | ffmpeg 转码（ASR 前转 mp3）与音频切分；`ffprobe` 探测仍固定 30 秒 |
| `recover_reply` | `60` | 处理中途失败后「补救回复」的单次等待。失败路径不该再吃满总预算，所以它比 `event_handle` 小得多 |

**取值顺序（唯一硬约束）**：`onebot_api(10) < recover_reply(60) < ffmpeg(120) < 单步上限(240) < LLM 单次(600) < event_handle(1200)`。

所有内层超时都发生在 `event_handle` 之内，所以**内层一旦 ≥ 外层，那个内层就永远轮不到触发**（外层先把它掐掉），只是看起来配了、实际失效——这是最容易踩的坑。`test_features.py` 里有一项断言会检查这个顺序。

三点约定：

- **非法值自动回落**：写成 `"600s"`、`null`、`0` 或负数时会回落到默认值并打一条 warning，不会让程序起不来，也不会出现「每个请求立刻超时」。
- **LLM 单次请求超时不在这里**，仍在 `openai.request_timeout`（避免同一个值有两个来源，改一个不生效会坑人）。它当前是 `600`，小于 `event_handle`，所以模型卡住时会先由它报错并回一句人话。
- **只收业务级超时**：内部 HTTP 的 15/20/30 秒探测、轮询属于实现细节，刻意不暴露，否则配置项会膨胀成噪音。

**超时 / 异常真的发生时，用户会收到什么**

| 情形 | 用户看到 | 进上下文吗 |
|---|---|---|
| 内层单步超时（上传 / 视频 / 音频 / ffmpeg） | 这一轮照常回复，只在对应位置写「识别失败（原因）」 | 是，作为当轮用户消息的一部分 |
| LLM 调用失败 / 返回空内容 | 「（我这轮没能生成回复：模型响应超时 / 限流 / 鉴权失败…，可以稍后再试一次。）」 | 是，作为 assistant 消息 |
| 其他意外异常（归一化整体失败、工具循环崩溃） | 先做一次**补救回复**（≤ `recover_reply` 秒）：带着已有上下文请模型把话说完，并明确「没拿到的部分要说明，不许编」。补救成功就正常回复 | 是，用模型这条真实回复写入 memory |
| 补救回复也失败 | 退回「（…处理出错了，已经记进日志…）」 | 是，`_remember_failure()` 会补记一对 user / assistant |
| 整条事件超时（> `event_handle`） | 「（这条消息处理超过 1200 秒，我先放弃了…）」 | 是，`_remember_failure()` 会补记一对 user / assistant |
| 发送本身失败（NapCat 掉线等） | 什么都没有 | 是——记忆在发送前已写，模型会以为说过 |

整条事件超时是被 `wait_for` 取消的，协程已经死了，`entry` 可能半写、`messages` 已丢，所以**不做补救**，直接走后两句的固定文案。LLM 自身的超时/限流/鉴权同理：再调一次只会更糟，由 `_tool_loop` 当场分类成固定回复。

异常一律带 traceback 打日志（`logger.exception`），日志里含事件定位（`kind/user/group/mid`）但不含聊天正文——补救成功也一样，成功不代表没出过错。

调参参考：本机/局域网跑得快，可整体压到 `event_handle=240 / file_upload=120 / video_understand=120 / ffmpeg=60`；云服务器上行慢或常发 100MB 以上视频，再把 `file_upload` 与 `event_handle` 一起抬（两者之差要留够 = LLM 那 600 秒 + 收尾）。

### 8. 顶层

| 字段 | 当前值 | 说明 |
|---|---|---|
| `log_level` | `INFO` | 日志级别：`DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `cache_ttl_hours` | `336` | `cache/` 与 `downloads/` 文件保留时长（2 周），`0` = 不按时间清理 |
| `cache_max_gb` | `5` | 临时目录总容量上限，超出后从最旧开始删至 90%；`0` = 不限制 |
| `debug_message_segments` | `false` | 打印原始消息段（含聊天内容），仅排障时开启 |

---

## 运维工具

### 持久化数据查看与选择性清理

```powershell
venv\Scripts\python maintain.py --stats    # 只查看，不改动
venv\Scripts\python maintain.py            # 交互式菜单，可多选
venv\Scripts\python maintain.py 1 3 -y     # 直接执行编号 1、3（跳过确认）
venv\Scripts\python maintain.py --init     # 只做初始化 / 修复
```

可清理项：会话记忆（可细分到单会话 / 仅媒体索引，清空前自动备份）、`downloads/`、`cache/`、
系统临时 `qqbot-*`、方舟远端文件、容器日志、初始化修复。**不会碰 NapCat 登录态**。

### 方舟远端文件

```powershell
venv\Scripts\python cleanup_remote.py --stats   # 只看用量
venv\Scripts\python cleanup_remote.py           # 查看 → 确认 → 全量清理
```

### 测试

```powershell
venv\Scripts\python test_features.py    # 18 项功能测试
venv\Scripts\python test_video.py       # 43 项视频 / 网页 / 媒体链路测试
```

测试会真实调用 API，产生少量费用。`test_connectivity.py` 与 `verify_models_e2e.py` 是早期一次性
探测脚本，用于快速确认 Key 与模型是否可用。

### 日志

```bash
docker compose logs -f bot        # 服务器
docker compose logs -f napcat     # 协议端
```

---

## 常见问题

| 现象 | 原因与处理 |
|---|---|
| 反复重连、日志 1005 | `onebot.token` 与 NapCat 不一致；常见于复制时混入 Tab / 空格 |
| 引用消息取不到、每条都等 10 秒 | 历史版本在读循环里 await 业务逻辑。当前已改为队列 + worker，若复现请检查是否改回 |
| TTS / ASR 返回 403 | 火山控制台未开通对应资源：`seed-tts-2.0` / `volc.seedasr.*` |
| 网页功能报 Playwright 错误 | 未安装浏览器内核：`playwright install chromium` |
| 文档 / 语音理解失败 | 检查 ffmpeg 是否在 PATH；检查方舟 Key 与模型 ID |
| 上传返回 400 | 文件超过方舟单文件 512 MB，或 `media_download_max_mb` 设置过大 |
| 容器重启后要重新扫码 | `napcat/qq` 未持久化，或登录态过期 |
| 回复被 QQ 拦截 | 缩短 `max_reply_length`、增大 `cooldown_seconds` / `media_send_interval` |

---

## 已知限制

1. **非官方协议**：NapCat 模拟客户端登录，违反 QQ 服务协议，存在风控 / 封号风险，建议使用小号。
2. **文档无法回 QQ 重下**：文档消息段缺少 `busid`，远端过期后只能如实告知，不能像图片 / 视频那样重新拉取。
3. **转发消息内的媒体无法重下**：合并转发不保留可回查的消息 ID。
4. **网页视频成本较高**：需要先下载再送模型，单个视频可能耗时数十秒；可用 `page_media=false` 或调小上限关闭。
5. **记忆长期膨胀**：`summarize_threshold_tokens=800000` 实际不触发压缩，`recent` 会持续增长，最终触及 128K 上下文。
6. **TTS / ASR 依赖控制台开通**：未开通时相关工具会失败，但文本、视觉、搜索等能力不受影响。
7. **全局串行队列（机制限制）**：所有会话共用一个 worker 串行处理，一条慢消息（长视频、大文档）会让其他会话排队等待。现在由 `timeouts.event_handle` 兜底（默认 1200 秒）：超时的那条会被放弃并收到一句说明，worker 立即释放。若需彻底解决，要改成按会话分片并发。

---

## 风险与合规

- 本项目仅用于**个人学习与技术研究**，请遵守当地法律法规与腾讯服务协议。
- 使用非官方协议端可能导致账号被限制或封禁，**请勿使用主力账号**，并控制回复频率。
- `config.json` 含明文 API Key 与 WS Token，已被 `.gitignore` / `.dockerignore` 排除。请勿提交、截图或分享。
- API 调用会产生费用，请自行评估用量与预算。

## 许可

仅供个人学习使用，不作任何明示或暗示的担保。
