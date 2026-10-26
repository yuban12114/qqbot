"""配置加载模块：读取 config.json 并与默认值合并。"""
import json
import logging
import os
from copy import deepcopy
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULTS = {
    "onebot": {
        "ws_url": "ws://127.0.0.1:3001",
        "token": "",
        "bot_qq": "",
    },
    "openai": {
        "api_key": "",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "temperature": 0.8,
        "max_tokens": 4000,
        "request_timeout": 30,
        "system_prompt": "你是一个友好、简洁的QQ聊天助手。",
    },
    "reply_rules": {
        "enable_private": True,
        "enable_group": True,
        "group_at_only": True,
        "friend_whitelist": [],
        "group_whitelist": [],
        "ignore_self": True,
        "cooldown_seconds": 1.5,
        "max_reply_length": 4000,
        "group_sender_prefix": True,
        # 媒体分离回复：把 AI 产出（文字 / 图片 / 语音 / 文件）各自作为一条独立消息发出，
        # 而不是挤在同一个消息气泡里。设为 false 则退回「文本+图片+语音合并一条」的旧行为。
        "separate_media_replies": True,
        # 分离回复时，相邻两条媒体消息之间的最小发送间隔（秒），规避 QQ「发送过快」风控。
        "media_send_interval": 0.3,
        # 是否自动分析「群文件上传 / 私聊离线文件」通知。
        # 设为 false 后不再自动回复文件，但「引用某条文件消息并 @机器人」仍会分析
        # （走 reply 段 → get_msg → 下载 → 扩展名路由，不受此开关影响）。
        "reply_to_files": True,
    },
    "memory": {
        "enable": True,
        "mode": "accumulate",
        "max_history": 12,
        "summarize_threshold_tokens": 800000,
        "keep_recent_turns": 200,
        "enable_cache_log": True,
        "persistence": {
            "enable": True,
            "path": "memory.json",
            "save_interval_seconds": 30,
        },
    },
    # 多模态 / 工具服务层：由 agent 调用，负责"视觉理解 / 文生图 / 语音合成 / 语音识别"。
    # 留空 api_key 时，agent 自动降级为纯文本（bot 仍可正常运行）。
    # 视觉理解 / 文生图走 OpenAI 兼容端点（base_url + model）；
    # 语音合成 / 语音识别走豆包 openspeech.bytedance.com（独立鉴权体系）。
    "services": {
        "openai": {
            "api_key": "",
            "base_url": "https://api.openai.com/v1",
            "vision_model": "gpt-4o",
            # doubao-seed-2-1-lite-260915 实测同时支持 视觉/视频/文档/音频 四类输入，
            # 视频比 doubao-seed-evolving 快约 3 倍（11s vs 31s），故三者统一用同一模型。
            "video_model": "doubao-seed-2-1-lite-260915",    # 视频理解
            "video_fps": 0.3,                                # 视频预处理抽帧率（方舟预处理用）
            # 视频分析时是否把音轨作为独立 input_audio 一起送。
            # 全模态模型不会自动解析 video_url 里的音轨，关掉此项会「听不到视频声音」。
            "video_with_audio": True,
            # 远端文件保留策略："keep" = 不主动删（方舟 7 天自动过期，总容量 20GB，
            # file_id 可复用于多轮追问）；"immediate" = 用完即删（配额敏感场景）。
            "files_retention": "keep",
            # 远端文件保留天数（1~30，0=用方舟默认 7 天）。实测上传时传 expire_at(Unix 秒)
            # 生效。拉长到 30 天是为了多轮追问能长期直接复用 file_id，不必回 QQ 重下。
            "media_expire_days": 30,
            "audio_model": "doubao-seed-2-1-lite-260915",    # 音频理解（input_audio）
            "document_model": "doubao-seed-2-1-lite-260915",  # 文档理解（file 内容类型，按页拆图）
            "image_model": "doubao-seedream-5-0-flash-260915",
            # tts_model/asr_model 仅信息字段；实际模型由下方端点/resource_id 决定
            "tts_model": "seed-audio-1.0",
            "asr_model": "doubao-seed-asr-2.0",
            "tts_voice": "",                # seed-audio-1.0 暂不使用 speaker，保留配置位
            "image_size": "2K",
            "image_watermark": False,       # False: 不加"AI生成"水印
            "cache_dir": "cache",
            # ---- 豆包语音（openspeech.bytedance.com）专属配置 ----
            "voice_api_key": "",             # 语音专用 Key（TTS/ASR 共用；openspeech 不接受 ark Key，会 401）
            # TTS: /api/v3/tts/create（X-Api-Key + X-Api-Request-Id，无需 resource_id）
            "tts_url": "https://openspeech.bytedance.com/api/v3/tts/create",
            "tts_resource_id": "volc.seedtts.default",  # create 接口未使用，保留兼容
            # ASR: /api/v3/auc/bigmodel/submit + /query（资源号 volc.seedasr.auc）
            "asr_url": "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit",
            "asr_resource_id": "volc.seedasr.auc",
            "asr_language": "zh-CN",
        }
    },
    # 链接打开 / 网页抓取相关
    "web": {
        # 无头浏览器(Playwright)渲染：用于 React/Vue 等"JS 动态渲染(SPA)"站点——
        # 这类站点静态 HTML 只是空壳，必须执行 JS 才能拿到正文。
        "headless_browser": True,     # 关闭则只用"下载 HTML+剥标签"的传统方式（SPA 抓不到正文）
        "spa_text_threshold": 200,    # 传统抓取剥离后文字少于该值(且为 HTML) → 判定疑似 SPA，改用浏览器渲染
        "browser_timeout": 20,         # 浏览器渲染超时(秒)
        "wait_until": "load",          # 等待策略：load / domcontentloaded / networkidle
        "render_wait_ms": 1500,        # 页面 load 之后再额外等待的毫秒数，让 SPA 数据渲染完成
        # ---- 网页内嵌媒体分析（打开一个网页时，把页面里的视频/音频/图片也交给模型看）----
        "page_media": True,            # 关闭则只读网页文本，不分析页面内嵌媒体
        "media_max_videos": 1,         # 每个页面最多分析几个视频（平台播放器需下载，耗时长）
        "media_max_images": 1,         # 每个页面最多分析几张图片（og:image / 大图）
        "media_max_docs": 1,           # 每个页面最多分析几个文档（页面里挂的 PDF/Office 链接）
        "probe_content_type": True,    # 链接无扩展名时用 HEAD 探测 Content-Type（识别下载型直链）
        # ---- DeepSeek 官方搜索（自研引擎 POST /v1/tools/search）----
        # 优先于"抓搜索引擎 HTML + 正则"那套：官方接口、结构化返回、实测 0.3s 级响应。
        # **失败会自动回退到原有的 Bing→DuckDuckGo→百度→搜狗 链路**，那套始终保留作保底。
        "deepseek_search": {
            "enable": True,          # 关闭则只用原有的搜索引擎抓取链路
            "count": 5,              # 返回条数（1~50）
            "need_content": False,   # True 会连网页正文一起返回（更大更慢，但可省一次 fetch_url）
            "timeout": 30,           # 单次请求超时(秒)
        },
        # B站 Cookie（可选）：机房 IP 访问 B 站接口常被风控返回 412，
        # 填浏览器里的 Cookie（至少含 SESSDATA=xxx）可显著缓解。
        # 获取：浏览器登录 B 站 → F12 → Application/存储 → Cookies → 复制 SESSDATA 的值。
        "bilibili_cookie": "",
        "media_download_max_seconds": 180,  # 平台视频下载的时长上限(秒)，超时长的直接跳过
        # 体积上限。注意方舟 Files API 对视频有硬限制：默认存储空间 512MB。
        # 超过 512MB 会在方舟侧返回 400，本地再大也没用（要更大需传 TOS Bucket，上限 2GB）。
        "media_download_max_mb": 40,   # 平台视频下载的体积上限(MB)
    },
    # ---- 各环节超时（秒），集中在这里便于按环境调参（云服务器慢、本机快）----
    # config.json 是 bind mount，改完只需 restart，不必重建镜像。
    # 注意：这里只收「业务级、值得调」的超时；内部 HTTP 的 15/20/30 秒这类
    # 探测/轮询超时属于实现细节，刻意不暴露，否则配置项会膨胀成噪音。
    # 取值原则：内层 < 外层。所有内层超时都发生在 event_handle 之内，
    # 一旦内层 >= 外层，内层的值就永远轮不到触发（外层先把它掐掉），等于一条失效配置。
    # 当前按「外层 1200 = 单步 600 + 收尾/多轮工具 600」配：
    #   onebot_api(10) < ffmpeg(120) < file_upload(240) = video_understand(240)
    #   < LLM(openai.request_timeout=600) < event_handle(1200)
    "timeouts": {
        "onebot_api": 10,          # 单次 OneBot API 调用（get_msg/send_msg 等）的默认等待
        "event_handle": 1200,      # 单条消息「下载→理解→回复」的总耗时上限（安全网，不是 SLA）
        "file_upload": 240,        # 上传单个文件到方舟 Files API（100MB@3.5Mbps 约 240 秒）
        "video_understand": 240,   # 单次视频理解请求
        "ffmpeg": 120,             # ffmpeg 转码 / 音频切分（ffprobe 探测仍固定 30 秒）
        "recover_reply": 60,       # 处理中途失败后"补救回复"的单次等待（失败路径不该吃满总预算）
    },
    "log_level": "INFO",
    # 临时文件保留时长（小时）。downloads/ 与 cache/ 里超过此时长的文件会被定期清理，
    # 防止历史媒体/生成物把磁盘写满（写满会导致所有下载、上传、转码一起失败）。
    # 设 0 表示禁用自动清理。默认 2 周：本地文件是"远端失效后重传的原料"，
    # 留久一点能覆盖"隔几天再追问"的场景，同时仍会被下面的容量上限约束。
    "cache_ttl_hours": 336,
    # 临时目录总容量上限（GB）。只限制时间不够——连发几个大视频，两周就能攒几十 GB。
    # 超过后按最旧开始删，直到降到上限的 90%。设为 0 表示不限制。
    "cache_max_gb": 5,
    # 排查消息解析用：开启后会把收到的原始消息段打进日志（含聊天内容，用完请关掉）
    "debug_message_segments": False,
}


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并 override 到 base，返回新字典。"""
    result = deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def timeout_of(cfg: dict, key: str) -> float:
    """读取 timeouts.<key>（秒），非法值回落到默认并记 warning。

    为什么要容错：config.json 是手改的，写成 "600s"、null、0 或负数都可能出现。
    这些值直接进 asyncio.wait_for 会抛出难定位的 TypeError/ValueError，
    或者在 0 的情况下让每个请求立刻超时——那比用默认值危险得多。
    """
    default = float(DEFAULTS["timeouts"].get(key, 30))
    raw = (cfg.get("timeouts") or {}).get(key)
    if raw is None:
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        logger.warning("timeouts.%s 不是数字（%r），已回落到默认 %s 秒", key, raw, default)
        return default
    if val <= 0:
        logger.warning("timeouts.%s 必须大于 0（当前 %r），已回落到默认 %s 秒", key, raw, default)
        return default
    return val


def load_config(path: Path) -> dict:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"未找到配置文件: {path}")
    with path.open("r", encoding="utf-8") as f:
        user_cfg = json.load(f)
    cfg = _deep_merge(DEFAULTS, user_cfg)

    # 环境变量覆盖：让同一份 config.json 能在服务器与本地之间切换，不必改文件。
    # 服务器上配 ws://napcat:3001（容器服务名）；本地调试配 ws://127.0.0.1:3001。
    #   Windows PowerShell:  $env:NAPCAT_WS_URL="ws://127.0.0.1:3001"
    #   Windows CMD:         set NAPCAT_WS_URL=ws://127.0.0.1:3001
    #   Linux/macOS:         NAPCAT_WS_URL=ws://127.0.0.1:3001 python main.py
    env_ws = os.environ.get("NAPCAT_WS_URL", "").strip()
    if env_ws:
        cfg.setdefault("onebot", {})["ws_url"] = env_ws

    return cfg
