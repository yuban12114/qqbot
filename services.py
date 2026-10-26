"""多模态 / 工具服务层（agent 的"手"）。

视觉理解 / 文生图走 OpenAI 兼容端点（方舟 Ark / 智谱 / 通义 / 本地 vLLM 等，
可只改 base_url + model）；语音合成(TTS) / 语音识别(ASR) 走豆包独立域名
openspeech.bytedance.com（与方舟 Ark 不同的鉴权体系，使用 X-Api-Key +
X-Api-Request-Id）：
- describe_image : 图片 → 文字描述（视觉理解，OpenAI 兼容端点）。统一三式传入：
  本地图优先 Files API 上传(file_id) / 失败回退 Base64 内联 / 直链或 data URI 直接 image_url.url
- understand_video: 视频 → 文字描述（豆包视频理解，模型 `video_model`=`doubao-seed-evolving`）。
  统一三式传入：本地视频 Files API 上传(视频预处理)→video_url.file_id / Base64 内联回退 / 直链 video_url.url
- understand_audio: 音频 → 文字描述（模型 `audio_model`=`doubao-seed-2-1-lite-260915`；
  方舟 Chat 的 input_audio 内容类型）。本地音频优先 Files API 上传得 file_id，
  失败回退 Base64 内联(data+format)；直链用 url。
- understand_document: 文档(PDF/Office) → 文字理解与概括（独立模型 `document_model`=`doubao-seed-evolving`，
  方舟 Chat 的 file 内容类型，模型按页拆图理解）。统一三式传入：Files API 上传(等待处理)→file_id /
  Base64 内联→file_data(需带 filename) / 直链→file_url
- generate_image : 文字 → 图片文件（文生图，OpenAI 兼容端点）
- text_to_speech : 文字 → 语音文件（豆包 TTS V3，`/api/v3/ts/create`，模型 seed-audio-1.0）
- speech_to_text : 语音文件 → 文字（豆包 ASR 录音文件识别 V2，`/api/v3/auc/bigmodel/submit`+`/query`）
- web_search     : 无 key 联网搜索（Bing → DuckDuckGo → 百度 → 搜狗 链式兜底；百度/搜狗国内可达但常被反爬拦，作末级兜底）
- fetch_url      : 打开并读取网页/链接内容（剥离 HTML，供"发链接给 bot 看内容"；
  SPA 自动降级到无头浏览器渲染；二进制媒体直链直接给提示而不会返回乱码）
- fetch_page_media: 抓网页并提取其中**内嵌的媒体资源**（og:video/og:image/og:audio、
  <video>/<audio>/<source>、平台播放器 iframe）；静态 HTML 无媒体时用无头浏览器从 DOM 取
- download_media : 用 yt-dlp 把平台页面（B站/YouTube/抖音等）的视频下到本地，
  交给 understand_video 真正"看画面"（默认限 180s / 40MB，下载 480p 以内）

豆包语音相关注意点（已在本文件内兼容处理）：
- TTS/ASR 鉴权均用 X-Api-Key + X-Api-Request-Id（**无需 X-Api-Resource-Id**；
  TTS 的 create 接口连 resource_id 都不发，ASR 的 auc 接口发 volc.seedasr.auc）。
- **openspeech 不接受方舟 ark Key（返回 401）**，必须用语音专用 voice_api_key
  （TTS/ASR 共用同一把）。
- ASR 为异步文件识别：本地音频先用 ffmpeg 转 mp3 → 长音频再按 10 分钟一段切分
  （openspeech 无文件上传端点，内联 base64 有单请求体积上限）→ 每段 base64 提交任务
  → 轮询 /query 取结果（result.text）。需本机安装 ffmpeg/ffprobe 并加入 PATH。
- TTS 模型为 seed-audio-1.0，请求体用 text_prompt（无需 speaker，默认音色）。

所有需要 key 的能力在 key 缺失 / 调用失败时都会抛出 ServiceUnavailable，
由 agent 捕获并降级处理，bot 不会崩溃。
"""
import asyncio
import base64
import datetime
import gzip
import html as _html
import http.client
import json
import logging
import re
import shutil
import ssl
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
import zlib
from collections import OrderedDict
from html.parser import HTMLParser
from pathlib import Path

from config import timeout_of

# 通用浏览器 UA（抓取网页 / 调第三方 API 时用）
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
try:
    import brotli as _brotli
except ImportError:
    _brotli = None

# 视频扩展名 → MIME（Files API 上传时按容器类型声明 Content-Type）
_VIDEO_MIME = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".flv": "video/x-flv",
    ".wmv": "video/x-ms-wmv",
    ".m4v": "video/x-m4v",
    ".3gp": "video/3gpp",
    ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg",
    ".ts": "video/mp2t",
}

_AUDIO_MIME = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".amr": "audio/amr",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".aac": "audio/aac",
    ".wma": "audio/x-ms-wma",
    ".opus": "audio/ogg",
    ".3gp": "audio/3gpp",
    ".webm": "audio/webm",
    ".silk": "audio/silk",
}

_DOC_MIME = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".html": "text/html",
}

def _is_web_ref(s: str) -> bool:
    """判断一个字符串是否为可直接传入模型的"链接/数据URI"（无需走 Files API 上传）。"""
    return str(s).startswith(("http://", "https://", "data:"))


# 同一链接通常会被读两次：fetch_url 取正文 + fetch_page_media 取内嵌媒体。
# 缓存原始 HTML（短 TTL、小容量）避免为同一页面下载两遍（SPA 场景还省一次浏览器启动）。
_HTML_CACHE: "OrderedDict[str, tuple[float, str, str]]" = OrderedDict()
_HTML_CACHE_TTL = 90.0
_HTML_CACHE_MAX = 8


def _cache_get_html(url: str):
    """取缓存的 (html, ctype)；过期或不存在返回 None。"""
    item = _HTML_CACHE.get(url)
    if not item:
        return None
    ts, html_text, ctype = item
    if time.time() - ts > _HTML_CACHE_TTL:
        _HTML_CACHE.pop(url, None)
        return None
    return html_text, ctype


def _cache_put_html(url: str, html_text: str, ctype: str) -> None:
    """写入 HTML 缓存（LRU 淘汰）。"""
    if not html_text:
        return
    _HTML_CACHE[url] = (time.time(), html_text, ctype)
    while len(_HTML_CACHE) > _HTML_CACHE_MAX:
        _HTML_CACHE.popitem(last=False)


# 无头浏览器（Playwright）：用于执行 JS 渲染 React/Vue 等 SPA 站点。
# 仅在真正调用时才 import，避免未安装时模块加载失败。
try:
    from playwright.async_api import async_playwright as _async_playwright  # noqa: F401
    _HAVE_PLAYWRIGHT = True
except ImportError:
    _HAVE_PLAYWRIGHT = False

logger = logging.getLogger("QQAIBot.Services")


def _strip_html(html: str) -> str:
    """粗略剥离 HTML 标签并反转义实体，保留可见文本。"""
    html = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?is)<!--.*?-->", " ", html)
    html = re.sub(r"(?is)<[^>]+>", " ", html)
    return _html.unescape(html)


# --------------------------------------------------------------------------- #
# 网页内嵌媒体提取：从 HTML 里找出"能给模型看的资源"（视频/音频/图片/平台播放器）
# --------------------------------------------------------------------------- #
# 主流视频平台的播放器/页面域名 —— 命中则视为可交给 yt-dlp 解析下载
_PLATFORM_VIDEO_HINTS = (
    "bilibili.com", "b23.tv", "youtube.com", "youtu.be", "douyin.com",
    "iesdouyin.com", "kuaishou.com", "xiaohongshu.com", "vimeo.com",
    "ixigua.com", "youku.com", "iqiyi.com", "weibo.com", "mgtv.com",
    "sohu.com", "163.com", "qq.com",
)


def _is_platform_video(url: str) -> bool:
    """判断 URL 是否属于主流视频平台（可交给 yt-dlp 尝试解析）。"""
    low = url.lower()
    return any(h in low for h in _PLATFORM_VIDEO_HINTS)


# 精确到"视频页面"的路径特征（比域名更窄，避免把门户/新闻页误判成视频页）
# 例：bilibili.com/video/BVxxx 是视频页，而 bilibili.com/read/cvxxx 是专栏文章。
_VIDEO_PAGE_PATTERNS = (
    r"bilibili\.com/video/", r"^https?://b23\.tv/", r"bilibili\.com/bangumi/play/",
    r"youtube\.com/(watch|shorts)", r"youtu\.be/",
    r"douyin\.com/video/", r"iesdouyin\.com/share/video",
    r"kuaishou\.com/(short-video|photo)/", r"v\.kuaishou\.com/",
    r"vimeo\.com/\d", r"ixigua\.com/\d",
    r"youku\.com/v_show", r"iqiyi\.com/v_", r"mgtv\.com/b/",
    r"weibo\.com/tv/", r"weibo\.com/video_page",
    r"xiaohongshu\.com/explore/",
)


def _is_video_page(url: str) -> bool:
    """URL 是否为"视频播放页面"（页面本身不是媒体文件，但可用 yt-dlp 取出视频流）。"""
    low = (url or "").lower()
    return any(re.search(p, low) for p in _VIDEO_PAGE_PATTERNS)


# 网页里可能"挂着"的文档类型（下载链接 / 内嵌预览）。.txt/.md 之类走本地读取即可，不在此列。
_DOC_URL_EXTS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
                 ".rtf", ".odt", ".ods", ".odp", ".epub", ".csv"}


def _is_doc_url(url: str) -> bool:
    """判断 URL 是否指向一个文档文件（忽略 query/fragment）。"""
    low = (url or "").lower().split("?")[0].split("#")[0]
    return low.endswith(tuple(_DOC_URL_EXTS))


def _guess_ext_from_ctype(ctype: str) -> str | None:
    """由 Content-Type 推断文档扩展名（下载兜底时决定落地文件的后缀）。"""
    c = (ctype or "").lower().split(";")[0].strip()
    if c == "application/pdf":
        return ".pdf"
    if "wordprocessingml" in c:
        return ".docx"
    if "spreadsheetml" in c:
        return ".xlsx"
    if "presentationml" in c:
        return ".pptx"
    if c == "application/msword":
        return ".doc"
    if c == "application/vnd.ms-excel":
        return ".xls"
    if c == "application/vnd.ms-powerpoint":
        return ".ppt"
    return None


# 文档类 MIME：fetch_url 遇到这类响应不能按文本解码（否则是乱码），要明确告知
_DOC_MIME_HINTS = ("application/pdf", "application/msword", "application/rtf",
                   "application/epub", "vnd.ms-excel", "vnd.ms-powerpoint",
                   "wordprocessingml", "spreadsheetml", "presentationml",
                   "application/vnd.oasis.opendocument")


def _is_doc_mime(ctype: str) -> bool:
    c = (ctype or "").lower()
    return any(h in c for h in _DOC_MIME_HINTS)


# HTTPS 证书：本机 Python 自带的 CA 链不完整，访问部分站点会
# CERTIFICATE_VERIFY_FAILED（实测 arxiv.org / gov.cn 必现，B 站正常）。
# 装了 certifi 就用它的根证书包（纯数据包，无副作用）。
try:
    import certifi as _certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=_certifi.where())
except Exception:  # noqa: BLE001  (未装 certifi 或 CA 加载失败 → 用系统默认)
    _SSL_CONTEXT = None


def _urlopen(req, timeout: float | None = None, **kwargs):
    """统一的 urlopen 入口：自动带上 certifi 的 CA（若可用）。

    所有出网请求都走这里，避免各处重复传 context。
    """
    if _SSL_CONTEXT is not None:
        kwargs.setdefault("context", _SSL_CONTEXT)
    if timeout is not None:
        kwargs["timeout"] = timeout
    return urllib.request.urlopen(req, **kwargs)


# B 站 Cookie（SESSDATA 等）。机房 IP 访问 B 站接口很容易被风控返回 412，
# 带上登录后的 Cookie 能显著降低被拦截概率。由 Services.__init__ 从 config 注入。
_BILI_COOKIE = ""


def _bili_headers(extra: dict | None = None) -> dict:
    """构造访问 B 站用的请求头（浏览器化 + 可选 Cookie）。"""
    headers = {
        "User-Agent": _UA,
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if _BILI_COOKIE:
        headers["Cookie"] = _BILI_COOKIE
    if extra:
        headers.update(extra)
    return headers


def _decompress(raw: bytes, enc: str) -> bytes:
    """按 Content-Encoding 手动解压响应体。

    urllib 只有在请求头声明 Accept-Encoding 时才会协商压缩，但部分站点
    （如 B 站）会**强制**返回 gzip，urllib 不会替我们解压 —— 不处理就会把
    压缩字节当文本解析，得到空页面/乱码。
    """
    enc = (enc or "").lower()
    if not enc:
        return raw
    try:
        if "gzip" in enc:
            return gzip.decompress(raw)
        if "deflate" in enc:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
        if "br" in enc and _brotli is not None:
            return _brotli.decompress(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("解压响应失败(enc=%s): %s", enc, exc)
    return raw


# 采集普通 <img> 时跳过的图标类资源（logo/头像/统计像素/装饰箭头等，对模型没意义）
_IMG_SKIP_HINTS = ("logo", "icon", "avatar", "sprite", "spacer", "pixel",
                   "1x1", "blank.", "qrcode", "erweima", "placeholder",
                   "arrow", "btn", "button", "dot.", "shadow", "corner",
                   "search", "close", "share", "star", "loading", "spinner")


def _looks_like_icon(url: str) -> bool:
    low = (url or "").lower().split("?")[0]
    if low.endswith(".svg"):
        return True
    return any(h in low for h in _IMG_SKIP_HINTS)


# ffmpeg 常见安装位置（PATH 未刷新时兜底；yt-dlp 合并音视频轨必须用到）
_FFMPEG_FALLBACK_DIRS = (
    r"E:\ffmpeg-master-latest-win64-gpl\bin",
    r"C:\ffmpeg\bin",
    r"C:\Program Files\ffmpeg\bin",
)


def _find_ffmpeg_dir() -> str | None:
    """定位 ffmpeg 所在目录（优先 PATH，其次常见安装路径）。找不到返回 None。"""
    exe = shutil.which("ffmpeg")
    if exe:
        return str(Path(exe).parent)
    for d in _FFMPEG_FALLBACK_DIRS:
        if Path(d, "ffmpeg.exe").exists():
            return d
    return None


def _ffmpeg_exe(name: str = "ffmpeg") -> str | None:
    """返回 ffmpeg/ffprobe 可执行文件路径。

    先查 PATH；查不到就用已安装目录里的同名程序 —— 这样即使当前进程是在
    改 PATH 之前启动的（环境变量未刷新），也能正常工作。
    """
    exe = shutil.which(name)
    if exe:
        return exe
    d = _find_ffmpeg_dir()
    if d:
        for fn in (f"{name}.exe", name):
            cand = Path(d, fn)
            if cand.exists():
                return str(cand)
    return None


class _PageMediaParser(HTMLParser):
    """从网页 HTML 中提取内嵌媒体资源。

    覆盖四类来源（按可信度从高到低）：
      1) og / twitter 元信息：og:video、og:image、og:audio（社交平台/AI 抓取用的标准标注）
      2) 原生标签：<video src>、<video poster>、<audio src>、<source type=...>
      3) link rel=image_src / video_src
      4) <iframe> 里的平台播放器（B站/YouTube/抖音…）

    只做"发现"，不下载；URL 的相对路径由调用方用 urljoin 转绝对。
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.videos: list[str] = []
        self.audios: list[str] = []
        self.images: list[str] = []
        self.embeds: list[str] = []
        self.docs: list[str] = []
        self.title: str | None = None
        self._in_title = False

    @staticmethod
    def _add(bucket: list, url: str | None) -> None:
        u = (url or "").strip()
        if u and u not in bucket:
            bucket.append(u)

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        tag = tag.lower()
        if tag == "meta":
            prop = (a.get("property") or a.get("name") or "").lower()
            content = a.get("content")
            if not content:
                return
            if prop in ("og:video", "og:video:url", "og:video:secure_url",
                        "og:video:iframe", "twitter:player", "twitter:player:stream"):
                self._add(self.videos, content)
            elif prop in ("og:audio", "og:audio:url", "og:audio:secure_url"):
                self._add(self.audios, content)
            elif prop in ("og:image", "og:image:url", "og:image:secure_url",
                          "twitter:image", "twitter:image:src"):
                self._add(self.images, content)
            elif prop in ("og:title", "twitter:title"):
                if not self.title:
                    self.title = content.strip()
        elif tag == "link":
            rel = (a.get("rel") or "").lower()
            ltype = (a.get("type") or "").lower()
            if rel == "image_src":
                self._add(self.images, a.get("href"))
            elif rel == "video_src":
                self._add(self.videos, a.get("href"))
            elif rel == "alternate" and ("pdf" in ltype or "document" in ltype):
                self._add(self.docs, a.get("href"))
        elif tag == "video":
            self._add(self.videos, a.get("src"))
            self._add(self.images, a.get("poster"))
        elif tag == "audio":
            self._add(self.audios, a.get("src"))
        elif tag == "source":
            src = a.get("src")
            typ = (a.get("type") or "").lower()
            if not src:
                return
            if typ.startswith("audio"):
                self._add(self.audios, src)
            elif typ.startswith("video"):
                self._add(self.videos, src)
            else:
                # 无 type 时按扩展名猜
                low = src.lower().split("?")[0]
                if any(low.endswith(e) for e in _AUDIO_MIME):
                    self._add(self.audios, src)
                elif any(low.endswith(e) for e in _VIDEO_MIME):
                    self._add(self.videos, src)
        elif tag == "img":
            # 普通正文图（很多站点没有 og:image，只能靠 <img>）；懒加载用 data-src
            src = (a.get("src") or a.get("data-src") or a.get("data-original")
                   or a.get("data-lazy-src") or "").strip()
            if (src and not src.startswith("data:")
                    and not _looks_like_icon(src) and len(self.images) < 6):
                self._add(self.images, src)
        elif tag == "a":
            # 正文里的文档附件链接（"点击下载 PDF" 这类最典型的"网页上的文档"）
            href = (a.get("href") or "").strip()
            if href and _is_doc_url(href) and len(self.docs) < 4:
                self._add(self.docs, href)
        elif tag in ("embed", "object"):
            src = (a.get("src") or a.get("data") or "").strip()
            if src and _is_doc_url(src):
                self._add(self.docs, src)
        elif tag == "iframe":
            src = a.get("src")
            if not src:
                return
            if _is_doc_url(src):
                self._add(self.docs, src)       # 浏览器内嵌的文档预览
            elif _is_platform_video(src):
                self._add(self.embeds, src)
        elif tag == "title":
            self._in_title = True

    def handle_data(self, data):
        if self._in_title and not self.title:
            t = (data or "").strip()
            if t:
                self.title = t

    def handle_endtag(self, tag):
        if tag.lower() == "title":
            self._in_title = False



def _parse_bing_results(html: str, max_results: int = 5) -> list:
    """从 Bing 搜索结果页抽取「标题 + 摘要」（结构：<li class="b_algo"> 内 <h2> 标题、b_caption 摘要）。"""
    blocks = html.split('class="b_algo"')
    results = []
    for blk in blocks[1:]:
        m = re.search(r"<h2>(.*?)</h2>", blk, re.S)
        if not m:
            continue
        title = _strip_html(m.group(1)).strip()
        snippet = ""
        cap = re.search(r'class="b_caption">(.*?)</div>', blk, re.S)
        if cap:
            p = re.search(r"<p[^>]*>(.*?)</p>", cap.group(1), re.S)
            snippet = _strip_html(p.group(1) if p else cap.group(1)).strip()
        if title:
            results.append(f"{title}\n   {snippet}" if snippet else title)
        if len(results) >= max_results:
            break
    return results


def _h3_title_snippet(seg: str):
    """从一段（以 <h3 开头，或内含 <h3）的结果块中抽出 (标题, 摘要)。

    百度/搜狗的有机结果都形如 <h3><a>标题</a></h3> + 后续摘要文本，
    故抽标题统一取首个 <a> 文本，摘要取标题之后、下一个 <h3 之前的可见文本。
    """
    m = re.search(r"<h3[^>]*>.*?<a[^>]*>(.*?)</a>", seg, re.S)
    if m:
        title = _strip_html(m.group(1)).strip()
        tail = seg[m.end():]
    else:
        m2 = re.search(r"<h3[^>]*>(.*?)</h3>", seg, re.S)
        title = _strip_html(m2.group(1)).strip() if m2 else ""
        tail = seg
    snippet = _strip_html(re.split(r"<h3", tail, maxsplit=1)[0])
    snippet = re.sub(r"<[^>]*", "", snippet)  # 兜底：清掉任何残缺标签片段
    snippet = re.sub(r"\s+", " ", snippet).strip()
    return title, snippet


def _parse_baidu_results(html: str, max_results: int = 5) -> list:
    """抽取百度搜索结果的「标题 + 摘要」。

    注意：百度对裸 urllib 请求常返回反爬验证页（无 c-container / <h3），
    此时返回空列表，由上层自动降级到下一个搜索引擎，不会让 bot 崩溃。
    """
    if 'class="c-container"' in html:
        segs = html.split('<div class="c-container"')[1:]
    else:
        parts = html.split("<h3")
        segs = ["<h3" + p for p in parts[1:]]
    results = []
    for seg in segs:
        title, snippet = _h3_title_snippet(seg)
        if not title or len(title) < 2:
            continue
        if title in ("百度一下", "百度热搜", "相关搜索"):
            continue
        line = f"{title}\n   {snippet}" if snippet else title
        if line not in results:
            results.append(line)
        if len(results) >= max_results:
            break
    return results


def _parse_sogou_results(html: str, max_results: int = 5) -> list:
    """抽取搜狗搜索结果的「标题 + 摘要」。

    同样地，搜狗可能因反爬返回验证页（无 rb / <h3），交由上层降级。
    """
    if 'class="rb"' in html:
        segs = html.split('<div class="rb"')[1:]
    else:
        parts = html.split("<h3")
        segs = ["<h3" + p for p in parts[1:]]
    results = []
    for seg in segs:
        title, snippet = _h3_title_snippet(seg)
        if not title or len(title) < 2:
            continue
        line = f"{title}\n   {snippet}" if snippet else title
        if line not in results:
            results.append(line)
        if len(results) >= max_results:
            break
    return results


class ServiceUnavailable(Exception):
    """某项服务因未配置 / 调用失败而不可用时抛出。"""


class Services:
    def __init__(self, cfg: dict):
        # cfg 这里是「完整配置」。services.openai 是"手"的专用配置；
        # 顶层 openai 是"大脑"(LLM) 的配置。
        svc_cfg = (cfg.get("services", {}) or {}).get("openai", {}) or {}
        brain = cfg.get("openai", {}) or {}

        # 缺省继承：当 services.openai 未单独配置 api_key 与 base_url 时，
        # 复用 brain(openai 段) 的凭据，实现"单 key 全家桶"
        # （例如把 brain 也指向豆包，services 这里留空即可共用同一 key）。
        inherit = not ((svc_cfg.get("api_key") or "").strip()
                       or (svc_cfg.get("base_url") or "").strip())
        if inherit:
            self.api_key = (brain.get("api_key") or "").strip()
            self.base_url = (brain.get("base_url") or "https://api.openai.com/v1").rstrip("/")
        else:
            self.api_key = (svc_cfg.get("api_key") or "").strip()
            self.base_url = (svc_cfg.get("base_url") or "https://api.openai.com/v1").rstrip("/")

        self.vision_model = svc_cfg.get("vision_model", "gpt-4o")
        self.video_model = svc_cfg.get("video_model", "")
        self.video_fps = float(svc_cfg.get("video_fps", 0.3))
        # 视频分析时是否同时把音轨作为独立 input_audio 模态送入。
        # 实测：全模态模型不会自动解析 video_url 里的音轨，三种传法只有
        # 「video_url + 独立 input_audio」能听到声音（880Hz 测试音可准确识别）。
        self.video_with_audio = bool(svc_cfg.get("video_with_audio", True))
        # 远端文件保留策略：
        #   "keep"（默认）= 不主动删，靠方舟 7 天自动过期；file_id 可在多轮追问中复用
        #   "immediate"   = 每次分析完立即删除（配额敏感时用，但会失去复用能力）
        # 注：Files API 总容量 20GB（512MB 只是单文件上限），7 天过期，正常使用无需频繁清理。
        self.files_retention = str(svc_cfg.get("files_retention", "keep")).strip().lower()
        # 远端文件保留天数（1~30，0 = 沿用方舟默认 7 天）。实测上传时传 expire_at（Unix 秒）
        # 可生效：created_at 与 expire_at 相差正好 N*86400。拉长到 30 天是为了让
        # 多轮追问（inspect_media）能长期直接复用 file_id，不必回 QQ 重新下载。
        try:
            self.media_expire_days = int(svc_cfg.get("media_expire_days", 30) or 0)
        except (TypeError, ValueError):
            self.media_expire_days = 30
        self.media_expire_days = max(0, min(30, self.media_expire_days))
        # 音频 / 文档理解模型：与视频模型独立。
        # 实测 doubao-seed-evolving 不支持 audio 输入，音频需走 doubao-seed-2-1-lite-260915（input_audio 类型）；
        # 文档（file 内容类型）doubao-seed-evolving 即可，base64(file_data)/直链(file_url)/上传(file_id) 三种均可用。
        self.audio_model = svc_cfg.get("audio_model", "")
        self.document_model = svc_cfg.get("document_model", "")
        self.image_model = svc_cfg.get("image_model", "gpt-image-1")
        self.tts_model = svc_cfg.get("tts_model", "seed-audio-1.0")
        self.asr_model = svc_cfg.get("asr_model", "whisper-1")
        self.tts_voice = svc_cfg.get("tts_voice", "")
        self.image_size = svc_cfg.get("image_size", "1024x1024")
        # False: 不在图片上加"AI生成"水印；True: 右下角加标识（方舟默认 true）
        self.image_watermark = bool(svc_cfg.get("image_watermark", False))

        # 超时集中配置（config.json 的 timeouts 段）。非法值由 timeout_of 回落默认。
        # 只收「业务级」超时；内部 15/20/30 秒的探测、轮询属实现细节，不暴露。
        self.upload_timeout = timeout_of(cfg, "file_upload")
        self.video_timeout = timeout_of(cfg, "video_understand")
        self.ffmpeg_timeout = timeout_of(cfg, "ffmpeg")

        # ---- 豆包语音（openspeech.bytedance.com）独立鉴权/端点 ----
        # TTS: /api/v3/tts/create（seed-audio-1.0，X-Api-Key + X-Api-Request-Id，无需 resource_id）
        # ASR: /api/v3/auc/bigmodel/submit + /query（资源号 volc.seedasr.auc）
        # 注意：openspeech 不接受方舟 ark Key（返回 401），必须用语音专用 voice_api_key。
        self.voice_api_key = (svc_cfg.get("voice_api_key") or self.api_key).strip()
        self.tts_url = svc_cfg.get("tts_url", "https://openspeech.bytedance.com/api/v3/tts/create")
        self.tts_resource_id = svc_cfg.get("tts_resource_id", "volc.seedtts.default")  # create 接口未使用，保留兼容
        self.asr_submit_url = svc_cfg.get("asr_url", "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit")
        self.asr_query_url = self.asr_submit_url.replace("/submit", "/query")
        self.asr_resource_id = svc_cfg.get("asr_resource_id", "volc.seedasr.auc")
        self.asr_language = svc_cfg.get("asr_language", "zh-CN")
        self.voice_ready = bool(self.voice_api_key)

        self.cache_dir = Path(__file__).parent / (svc_cfg.get("cache_dir") or "cache")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.openai_ready = bool(self.api_key)
        # 各多模态理解能力就绪标志：需要方舟 api_key 且配置了对应模型
        self.video_ready = bool(self.api_key) and bool(self.video_model)
        self.audio_ready = bool(self.api_key) and bool(self.audio_model)
        self.document_ready = bool(self.api_key) and bool(self.document_model)

        # 链接打开 / 网页抓取相关配置（含无头浏览器 SPA 渲染开关）
        self.web_config = cfg.get("web", {}) or {}

        # DeepSeek 官方搜索（自研引擎）：复用大脑那套 api_key / base_url（config.openai），
        # 与方舟多模态（services.openai）是两套独立的凭据。
        llm_cfg = cfg.get("openai") or {}
        self.ds_api_key = str(llm_cfg.get("api_key") or "").strip()
        self.ds_base_url = str(llm_cfg.get("base_url") or "https://api.deepseek.com").rstrip("/")
        _ds = (cfg.get("web") or {}).get("deepseek_search") or {}
        self.ds_search_enable = bool(_ds.get("enable", True))
        try:
            self.ds_search_count = max(1, min(50, int(_ds.get("count", 5) or 5)))
        except (TypeError, ValueError):
            self.ds_search_count = 5
        self.ds_search_content = bool(_ds.get("need_content", False))
        try:
            self.ds_search_timeout = float(_ds.get("timeout", 30) or 30)
        except (TypeError, ValueError):
            self.ds_search_timeout = 30.0

        # B 站 Cookie 注入到模块级变量（_fetch_bilibili / download_media 是 staticmethod）
        global _BILI_COOKIE
        _BILI_COOKIE = str(self.web_config.get("bilibili_cookie") or "").strip()

    # ----------------------------- 基础请求 -----------------------------
    def _post_json(self, path: str, payload: dict, timeout: float = 60) -> dict:
        if not self.api_key:
            raise ServiceUnavailable("未配置 OpenAI api_key")
        url = f"{self.base_url}/{path.lstrip('/')}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {self.api_key}")
        with _urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post_bytes(self, path: str, payload: dict, timeout: float = 60) -> bytes:
        if not self.api_key:
            raise ServiceUnavailable("未配置 OpenAI api_key")
        url = f"{self.base_url}/{path.lstrip('/')}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {self.api_key}")
        with _urlopen(req, timeout=timeout) as resp:
            return resp.read()

    def _post_multipart(self, path: str, fields: dict, files: dict, timeout: float = 90) -> dict:
        if not self.api_key:
            raise ServiceUnavailable("未配置 OpenAI api_key")
        url = f"{self.base_url}/{path.lstrip('/')}"
        boundary = "----wb" + uuid.uuid4().hex
        parts = []
        for k, v in fields.items():
            parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{k}"\r\n\r\n{str(v)}\r\n'.encode("utf-8")
            )
        for k, (fname, data, ctype) in files.items():
            parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{k}"; filename="{fname}"\r\n'
                f"Content-Type: {ctype}\r\n\r\n".encode("utf-8") + data + b"\r\n"
            )
        body = b"".join(parts) + f"--{boundary}--\r\n".encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        req.add_header("Authorization", f"Bearer {self.api_key}")
        with _urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post_multipart_stream(self, path: str, fields: dict, file_field: str,
                               file_path: Path, ctype: str,
                               timeout: float = 300) -> dict:
        """流式 multipart 上传：文件分块发送，**不在内存里拼出完整请求体**。

        为什么需要：`_post_multipart` 会 `read_bytes()` 读成一份，再塞进 parts 复制
        一份，`b"".join()` 又复制一份 —— 内存放大 3 倍。1GB 视频要 3GB 内存，
        2~4G 的机器直接 OOM。这里用 http.client 手工发请求，峰值只占 256KB 缓冲区。

        Content-Length 必须精确（流式发送无法回退），所以先 stat 拿文件大小。
        如果上传期间文件被改动（大小变了）会中止，避免请求体长度与声明不符。
        """
        target = urllib.parse.urlparse(f"{self.base_url}/{path.lstrip('/')}")
        boundary = "----wb" + uuid.uuid4().hex

        head = b""
        for k, v in fields.items():
            head += (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{k}"\r\n\r\n{str(v)}\r\n'
            ).encode("utf-8")
        head += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{file_field}"; filename="{file_path.name}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n"
        ).encode("utf-8")
        tail = f"\r\n--{boundary}--\r\n".encode("utf-8")

        file_size = file_path.stat().st_size
        total = len(head) + file_size + len(tail)

        conn_cls = (http.client.HTTPSConnection if target.scheme == "https"
                    else http.client.HTTPConnection)
        kwargs = {"timeout": timeout}
        if target.scheme == "https" and _SSL_CONTEXT is not None:
            kwargs["context"] = _SSL_CONTEXT

        conn = conn_cls(target.hostname, target.port, **kwargs)
        try:
            conn.putrequest("POST", target.path or "/")
            conn.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
            conn.putheader("Content-Length", str(total))
            conn.putheader("Authorization", f"Bearer {self.api_key}")
            conn.endheaders()
            conn.send(head)

            sent = 0
            with file_path.open("rb") as f:
                while True:
                    chunk = f.read(256 * 1024)
                    if not chunk:
                        break
                    conn.send(chunk)
                    sent += len(chunk)
            if sent != file_size:
                raise ServiceUnavailable(
                    f"上传期间文件大小发生变化（声明 {file_size} 实发 {sent}），已中止")
            conn.send(tail)

            resp = conn.getresponse()
            body = resp.read()
            status, reason = resp.status, resp.reason
        finally:
            conn.close()

        if status >= 400:
            raise ServiceUnavailable(
                f"HTTP {status} {reason} | body={body[:300].decode('utf-8', 'ignore')}")
        try:
            return json.loads(body.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise ServiceUnavailable(f"上传响应解析失败: {exc}")

    def _post_json_raw(self, url: str, payload: dict, headers: dict, timeout: float = 60) -> dict:
        """带任意请求头的 JSON POST（用于豆包 openspeech 的 X-Api-Key 鉴权）。"""
        if not url:
            raise ServiceUnavailable("未配置语音服务地址")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with _urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "ignore")
            except Exception:  # noqa: BLE001
                pass
            raise ServiceUnavailable(f"HTTP {e.code}: {e.reason} | body={detail[:300]}")

    def _post_text_raw(self, url: str, payload: dict, headers: dict, timeout: float = 60) -> str:
        """带任意请求头的 POST，返回原始响应文本（用于 V3 流式 TTS 的多段 JSON）。"""
        if not url:
            raise ServiceUnavailable("未配置语音服务地址")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        for k, v in headers.items():
            req.add_header(k, v)
        with _urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="ignore")

    # ----------------------------- 图片本地化 -----------------------------
    async def _localize_image(self, image_path_or_url: str) -> str:
        """把图片变成可读取的本地路径（远程 URL 先下载到缓存）。"""
        if image_path_or_url.startswith(("http://", "https://")):
            return await asyncio.to_thread(self._download, image_path_or_url)
        p = Path(image_path_or_url)
        if p.exists():
            return str(p)
        raise ServiceUnavailable(f"图片路径不存在: {image_path_or_url}")

    def _download(self, url: str) -> str:
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", "Mozilla/5.0")
        suffix = ".bin"
        if url.lower().split("?")[0].endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            suffix = Path(urllib.parse.urlparse(url).path).suffix or ".png"
        with _urlopen(req, timeout=30) as resp:
            data = resp.read()
        out = self.cache_dir / f"dl_{uuid.uuid4().hex}{suffix}"
        out.write_bytes(data)
        return str(out)

    def _download_bytes(self, url: str) -> bytes:
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", "Mozilla/5.0")
        with _urlopen(req, timeout=30) as resp:
            return resp.read()

    # ----------------------------- 视觉理解 -----------------------------
    # --------------------- 统一多模态文件接入（Files API + Base64 + 直链） ---------------------
    def _guess_mime(self, path: Path) -> str:
        ext = path.suffix.lower()
        return (_VIDEO_MIME.get(ext) or _AUDIO_MIME.get(ext)
                or _DOC_MIME.get(ext) or "application/octet-stream")

    def _upload_file(self, path: Path, purpose: str = "user_data",
                    preprocess_configs: dict | None = None) -> str:
        """方舟 Files API 通用上传（multipart，**流式**），返回 file_id。

        视频可传入 preprocess_configs（声明预处理模型与抽帧率）；图片/音频/文档一般无需预处理。
        走 `_post_multipart_stream` 而不是 `_post_multipart`：后者会把文件读成 bytes
        再复制两份，内存放大 3 倍，大视频必然 OOM。
        """
        ctype = self._guess_mime(path)
        fields = {"purpose": purpose}
        # 显式指定保留期（实测接受 Unix 秒级时间戳，1~30 天）。默认 7 天，
        # 拉长到 30 天可让多轮追问在更长时间内直接复用 file_id，避免重复上传。
        if self.media_expire_days > 0:
            fields["expire_at"] = str(int(time.time()) + self.media_expire_days * 86400)
        if preprocess_configs:
            fields["preprocess_configs"] = json.dumps(preprocess_configs, ensure_ascii=False)
        try:
            resp = self._post_multipart_stream(
                "files", fields, "file", path, ctype, timeout=self.upload_timeout)
        except ServiceUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ServiceUnavailable(f"文件上传(方舟 Files API)失败: {exc}")
        fid = resp.get("id")
        if not fid:
            raise ServiceUnavailable(f"文件上传未返回 id：{resp}")
        return fid

    def _upload_video_file(self, path: Path, fps: float) -> str:
        """视频上传：在通用上传基础上带视频预处理（抽帧）。"""
        return self._upload_file(
            path, purpose="user_data",
            preprocess_configs={"video": {"model": self.video_model, "fps": fps}},
        )

    async def _ingest_media(self, path_or_url: str, *, is_url: bool = False,
                            content_type: str = "image_url",
                            purpose: str = "user_data",
                            preprocess_configs: dict | None = None,
                            wait: bool = False,
                            mime_fallback: str = "application/octet-stream") -> dict:
        """把任意来源的文件转成 chat 模型可用的"引用" dict，统一支持三种传入方式。

        不同内容类型的引用字段不同（已按方舟实测修正）：
          - image_url / video_url：直链/内联用 {"url": ...}；Files API 上传得 {"file_id": ...}
          - file（文档）：直链 {"file_url": ...}；内联 {"file_data": "data:...", "filename": ...}；
                        上传 {"file_id": ...}
          - input_audio（音频）：直链 {"url": ...}；本地文件优先上传 {"file_id": ...}，
                        失败回退内联 {"data": b64, "format": ext}
        """
        # 音频：优先 Files API（file_id），失败回退内联；直链用 url
        if content_type == "input_audio":
            if is_url or _is_web_ref(path_or_url):
                return {"url": path_or_url}
            p = Path(path_or_url)
            if not p.exists():
                raise ServiceUnavailable(f"音频文件不存在: {path_or_url}")

            tmp_path = None
            try:
                ext = (p.suffix.lstrip(".") or "wav").lower()
                # QQ 语音/音频多为 ogg(opus) / amr / silk，方舟音频模型对这类容器+编码
                # 组合支持不稳（实测 ogg 直接 HTTP 400）。统一用 ffmpeg 转 mp3 再处理。
                if ext in ("ogg", "opus", "amr", "silk", "m4a", "aac", "wma", "webm"):
                    raw = await asyncio.to_thread(self._to_mp3, p)
                    tmp_path = p.parent / f"_conv_{uuid.uuid4().hex}.mp3"
                    tmp_path.write_bytes(raw)
                    logger.info("音频 %s 已转 mp3（%d KB）", p.name, len(raw) // 1024)
                    p = tmp_path
                    ext = "mp3"

                # 优先走 Files API 流式上传（实测 input_audio 支持 file_id）：
                # 内存只占 256KB 分块，长音频不会因 base64 放大而撑爆。
                try:
                    fid = await asyncio.to_thread(
                        self._upload_file, p, purpose, preprocess_configs)
                    return {"file_id": fid}
                except Exception as exc:  # noqa: BLE001
                    size_mb = p.stat().st_size / 1048576
                    if size_mb > 200:
                        raise ServiceUnavailable(
                            f"音频 {size_mb:.0f}MB 上传失败，为避免 OOM 不做 Base64 回退：{exc}")
                    logger.warning("音频上传失败(%s)，回退内联 Base64", exc)

                raw = p.read_bytes()
                b64 = base64.b64encode(raw).decode("ascii")
                return {"data": b64, "format": ext}
            except ServiceUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001
                raise ServiceUnavailable(f"音频读取/编码失败: {exc}")
            finally:
                if tmp_path is not None:
                    tmp_path.unlink(missing_ok=True)

        # 其余类型：远程直链
        if is_url or _is_web_ref(path_or_url):
            if content_type == "file":
                return {"file_url": path_or_url}
            return {"url": path_or_url}

        # 本地文件：优先方舟 Files API 上传
        p = Path(path_or_url)
        if not p.exists():
            raise ServiceUnavailable(f"文件不存在: {path_or_url}")
        try:
            fid = await asyncio.to_thread(self._upload_file, p, purpose, preprocess_configs)
        except Exception as exc:  # noqa: BLE001
            # 只有「上传本身」失败才回退内联。预处理失败必须单独处理（见下），
            # 否则大文件会被 base64 化导致 OOM，且已上传的远端文件会泄漏。
            logger.warning("Files API 上传失败，回退内联编码: %s", exc)
            # 大文件禁止 Base64 回退：raw + b64 + JSON body 会放大到 3 倍以上，
            # 一个 1GB 视频能吃掉 3GB+ 内存，宁可报错也不要拖垮整个进程。
            try:
                size_mb = p.stat().st_size / 1024 / 1024
            except OSError:
                size_mb = 0
            if size_mb > 200:
                raise ServiceUnavailable(
                    f"文件 {size_mb:.0f}MB 上传失败，为避免 OOM 不做 Base64 回退：{exc}")
            try:
                raw = p.read_bytes()
                b64 = base64.b64encode(raw).decode("ascii")
                mime = self._guess_mime(p) if mime_fallback == "application/octet-stream" else mime_fallback
                if content_type == "file":
                    return {"file_data": f"data:{mime};base64,{b64}", "filename": p.name}
                # image_url / video_url
                return {"url": f"data:{mime};base64,{b64}"}
            except Exception as e2:  # noqa: BLE001
                raise ServiceUnavailable(f"文件读取/编码失败: {e2}")

        # 上传已成功：此时远端已有文件，任何后续失败都必须先回收，且**不能**退回 base64
        # （预处理失败代表文件太大/格式问题，重试内联只会更糟）。
        if wait:
            try:
                await asyncio.to_thread(self._wait_file_processed, fid)
            except Exception as exc:  # noqa: BLE001
                try:
                    await asyncio.to_thread(self._delete_file, fid)
                except Exception:  # noqa: BLE001
                    pass
                raise ServiceUnavailable(
                    f"文件预处理失败/超时（文件过大或格式不支持）：{exc}")
        return {"file_id": fid}

    async def _chat_with_media(self, ref: dict, content_type: str, model: str,
                               prompt: str, *, fps: float | None = None,
                               max_tokens: int = 2000,
                               timeout: float = 120,
                               extra_parts: list | None = None) -> str:
        """构造 chat/completions 请求：一个主媒体引用 + 可选额外媒体段 + 文本 prompt。

        content_type 取值：image_url / video_url / file / input_audio。
        extra_parts 用于「同一请求里带多个模态」——例如视频 + 独立音轨（全模态模型
        不会自动解析 video_url 里的音轨，必须把音频作为单独的 input_audio 一起送）。
        timeout 为单次 HTTP 读超时：长文档/长视频的推理耗时远高于图片，需单独放大
        （实测 30 页 PDF 用 60s 会 Read timed out）。
        """
        media_part = {"type": content_type, content_type: ref}
        if fps is not None and content_type == "video_url":
            media_part["video_url"]["fps"] = fps
        content = [media_part]
        if extra_parts:
            content.extend(extra_parts)
        content.append({"type": "text", "text": prompt})
        messages = [{"role": "user", "content": content}]
        try:
            resp = await asyncio.to_thread(
                self._post_json, "chat/completions",
                {"model": model, "messages": messages, "max_tokens": max_tokens},
                timeout)
            return (resp["choices"][0]["message"]["content"] or "").strip()
        except ServiceUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ServiceUnavailable(f"多模态推理失败: {exc}")

    # ----------------------------- 图片理解 -----------------------------
    async def describe_image(self, image_path_or_url: str, prompt: str | None = None, *,
                             is_url: bool = False, sink: dict | None = None) -> str:
        """图片理解。prompt 为 None 时用下面的默认描述提示。

        之所以留成参数：首轮可以让调用方把"用户真正想问的"拼进来（见 agent._first_prompt），
        让视觉模型直接回答用户关心的地方，而不是每次都输出一段通用描述。
        """
        if not self.openai_ready:
            raise ServiceUnavailable("未配置 api_key，无法做视觉理解")
        prompt = prompt or ("请客观描述这张图片的关键内容、文字、人物或场景，用中文简洁说明。")
        ref = await self._ingest_media(image_path_or_url, is_url=is_url, purpose="user_data", wait=False)
        # sink 用于把引用（file_id）带出函数，供上层登记到会话的 media 表，
        # 之后追问时可直接复用，不必重新上传。不改返回值，避免影响现有调用点。
        if sink is not None:
            sink["ref"] = ref
        try:
            return await self._chat_with_media(ref, "image_url", self.vision_model, prompt, max_tokens=1200)
        finally:
            await self._cleanup_remote(ref)

    # ----------------------------- 视频理解（豆包原生视频端点） -----------------------------
    async def understand_video(self, path_or_url: str, prompt: str | None = None,
                              is_url: bool = False, fps: float | None = None,
                              with_audio: bool | None = None,
                              sink: dict | None = None) -> str:
        """视频理解（模型 self.video_model）。统一三种传入：
          1) 本地视频：Files API 上传(视频预处理) → 等待完成 → video_url.file_id；失败回退 Base64 内联。
          2) 视频直链/数据URI：直接 video_url.url（可选 fps）。

        with_audio（默认取 config `services.openai.video_with_audio`）：
        本地视频会额外抽出音轨作为**独立的 input_audio 模态**一起送，
        否则模型听不到视频里的声音（它不会自动解析 video_url 内的音轨）。
        远端直链无本地文件可抽，只能按画面分析。
        """
        if not self.api_key:
            raise ServiceUnavailable("未配置 api_key，无法做视频理解")
        if not self.video_model:
            raise ServiceUnavailable("未配置 video_model，无法做视频理解（请在 services.openai 配置）")
        if prompt is None:
            prompt = ("请综合**画面与声音**描述这个视频：发生了什么、有哪些人物或物体、"
                      "主要事件与画面变化；如果有说话声、音乐或环境音，也要说明内容与情绪。"
                      "用中文简洁说明。")
        want_audio = self.video_with_audio if with_audio is None else bool(with_audio)
        remote = is_url or _is_web_ref(path_or_url)

        ref = await self._ingest_media(
            path_or_url, is_url=is_url, content_type="video_url", purpose="user_data",
            preprocess_configs={"video": {"model": self.video_model, "fps": fps or self.video_fps}},
            wait=(not remote),
        )

        audio_ref, audio_tmp = None, None
        if want_audio and not remote:
            audio_ref, audio_tmp = await self._extract_audio_for_video(Path(path_or_url))

        if sink is not None:
            sink["ref"] = ref
            # 视频的音轨是**独立上传的另一个 file_id**，必须一起登记；
            # 否则追问时只有画面、没有声音，等于退回"听不到"的老问题。
            if audio_ref:
                sink["audio_ref"] = audio_ref

        try:
            extra = [{"type": "input_audio", "input_audio": audio_ref}] if audio_ref else None
            return await self._chat_with_media(ref, "video_url", self.video_model, prompt,
                                               fps=(fps if remote else None),
                                               timeout=self.video_timeout,
                                               extra_parts=extra)
        finally:
            await self._cleanup_remote(ref)
            if audio_ref:
                await self._cleanup_remote(audio_ref)
            if audio_tmp is not None:
                audio_tmp.unlink(missing_ok=True)

    def _wait_file_processed(self, file_id: str, timeout_s: int = 180) -> dict:
        """轮询方舟 Files API 直到文件预处理完成（视频抽帧 / PDF 分页等）。"""
        deadline = time.time() + timeout_s
        last = {}
        while time.time() < deadline:
            try:
                last = self._get_json(f"files/{file_id}", timeout=20)
            except Exception as exc:  # noqa: BLE001
                logger.warning("查询文件处理状态失败: %s", exc)
                time.sleep(2)
                continue
            status = str(last.get("status") or "").lower()
            # 方舟 Files API 文件处理完成后的状态实测为 "active"（也兼容其他常见写法）
            if status in ("active", "processed", "processing_completed", "completed",
                          "ready", "succeeded", "available"):
                return last
            if "fail" in status or "error" in status:
                raise ServiceUnavailable(f"文件预处理失败：status={status} {last}")
            time.sleep(2)
        raise ServiceUnavailable(f"文件预处理超时（方舟处理较慢，请稍后重试或换更小文件）：最后状态={last.get('status')}")

    def _delete_file(self, file_id: str) -> None:
        """删除方舟 Files API 上的远端文件（DELETE /files/{id}）。

        为什么必须删：Files API 默认存储空间只有 **512MB**，而我们每次分析都会上传一份，
        从不清理的话几天就会占满，之后所有上传都返回 400。
        本项目每次都是"上传→用完即弃"，不存在复用 file_id 的场景，故用完即删是安全的。
        """
        if not self.api_key or not file_id:
            return
        url = f"{self.base_url}/files/{file_id}"
        req = urllib.request.Request(url, method="DELETE")
        req.add_header("Authorization", f"Bearer {self.api_key}")
        with _urlopen(req, timeout=20) as resp:
            resp.read()
        logger.debug("已删除远端文件 %s", file_id)

    async def _cleanup_remote(self, ref: dict) -> None:
        """按 `files_retention` 策略决定是否清理远端文件。

        默认 **不删**：方舟 Files API 文件默认保留 **7 天**后自动过期清理
        （`expire_at` 可设 1~30 天），且总容量 **20GB**（512MB 只是单文件上限）。
        保留 file_id 的价值：多轮追问（harness）时可直接复用，无需重复上传大文件。
        只有把 `services.openai.files_retention` 设为 "immediate" 才会立即删除。
        """
        if self.files_retention != "immediate":
            return
        fid = (ref or {}).get("file_id")
        if not fid:
            return
        try:
            await asyncio.to_thread(self._delete_file, fid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("清理远端文件失败(%s): %s", fid, exc)

    def files_usage(self, max_pages: int = 10) -> tuple[int, int]:
        """查询远端存储用量，返回 (文件数, 总字节数)。

        用于配额监控：总容量 20GB，接近时提醒清理。分页用 `after` 游标，
        并做 id 去重（部分实现不支持 after 时会重复返回首页，避免重复计数）。
        """
        if not self.api_key:
            return 0, 0
        count, total = 0, 0
        seen: set = set()
        after = None
        for _ in range(max_pages):
            q = "files?limit=100" + (f"&after={after}" if after else "")
            try:
                info = self._get_json(q) or {}
            except Exception:  # noqa: BLE001
                break
            items = info.get("data") or []
            fresh = [f for f in items if f.get("id") and f["id"] not in seen]
            if not fresh:
                break
            for f in fresh:
                seen.add(f["id"])
                total += int(f.get("bytes") or 0)
                count += 1
            after = fresh[-1]["id"]
            if len(items) < 100:
                break
        return count, total

    def _get_json(self, path: str, timeout: float = 20) -> dict:
        """带 Bearer 鉴权的 GET 请求（用于 Files API 状态轮询）。"""
        if not self.api_key:
            raise ServiceUnavailable("未配置 api_key")
        url = f"{self.base_url}/{path.lstrip('/')}"
        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", f"Bearer {self.api_key}")
        with _urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # ----------------------------- 音频理解（走方舟 Chat 的 input_audio 内容类型） -----------------------------
    async def understand_audio(self, path_or_url: str, prompt: str | None = None,
                              is_url: bool = False, model: str | None = None,
                              sink: dict | None = None) -> str:
        """音频理解：走方舟 Chat 的 input_audio 内容类型（模型 self.audio_model）。

        实测：doubao-seed-evolving 不支持音频输入（报 "audio input is not supported by this model"），
        音频需用支持音频的模型（如 doubao-seed-2-1-lite-260915）。
        传入方式：本地音频优先 Files API 上传(file_id)，失败回退 Base64 内联(data+format)；
        音频直链用 url。ogg/opus/amr/silk 等先转 mp3（QQ 的 ogg 是 opus 编码，直接传会 400）。
        """
        if not self.api_key:
            raise ServiceUnavailable("未配置 api_key，无法做音频理解")
        model = model or self.audio_model
        if not model:
            raise ServiceUnavailable("未配置 audio_model，无法做音频理解（请在 services.openai 配置 audio_model）")
        prompt = prompt or ("请客观描述这段音频的关键内容：说了什么、有哪些人物或声音、"
                            "主要事件与情绪，用中文简洁说明。")
        ref = await self._ingest_media(path_or_url, is_url=is_url, content_type="input_audio")
        if sink is not None:
            sink["ref"] = ref
        try:
            return await self._chat_with_media(ref, "input_audio", model, prompt,
                                               max_tokens=2000, timeout=180)
        finally:
            await self._cleanup_remote(ref)

    # ----------------------------- 文档理解（走方舟 Chat 的 file 内容类型） -----------------------------
    async def understand_document(self, path_or_url: str, prompt: str | None = None,
                                  is_url: bool = False, model: str | None = None,
                                  sink: dict | None = None) -> str:
        """文档理解：PDF/Office 等交给方舟 Chat 的 file 内容类型（模型按页拆图理解），模型 self.document_model。
        实测 doubao-seed-evolving 即可，且三种传入方式均可用：
          - 本地文档 Files API 上传(等待处理) → file_id
          - Base64 内联 → file_data（需带 filename）
          - 直链 → file_url
        """
        if not self.api_key:
            raise ServiceUnavailable("未配置 api_key，无法做文档理解")
        model = model or self.document_model
        if not model:
            raise ServiceUnavailable("未配置 document_model，无法做文档理解（请在 services.openai 配置 document_model）")
        prompt = prompt or ("请阅读并理解这个文档，用中文概括其核心内容与要点；"
                            "若为 PDF/扫描件，请尽量提取可见文字与图表信息。")
        ref = await self._ingest_media(path_or_url, is_url=is_url, content_type="file",
                                       purpose="user_data", wait=True)
        if sink is not None:
            sink["ref"] = ref
        try:
            return await self._chat_with_media(ref, "file", model, prompt,
                                               max_tokens=4000, timeout=420)
        finally:
            await self._cleanup_remote(ref)

    # ----------------------------- 追问（复用已上传的引用） -----------------------------
    _ASK_CT = {"image": "image_url", "video": "video_url",
               "audio": "input_audio", "document": "file"}
    _ASK_TIMEOUT = {"image": 120, "video": 300, "audio": 180, "document": 420}

    async def ask_media(self, ref: dict, kind: str, question: str,
                        audio_ref: dict | None = None) -> str:
        """用**已存在的引用**追问细节，不重新上传（inspect_media 的核心）。

        ref 来自会话 media 表里登记的 file_id / url，因此这里只发一次 chat 请求；
        视频若登记了音轨 ref，会作为额外 input_audio 一起送，否则追问就"听不到声音"。
        """
        kind = (kind or "image").strip().lower()
        ct = self._ASK_CT.get(kind, "image_url")
        model = {"image": self.vision_model, "video": self.video_model,
                 "audio": self.audio_model, "document": self.document_model}.get(kind)
        if not model:
            raise ServiceUnavailable(f"未配置 {kind} 对应的理解模型，无法追问")
        prompt = ("请针对下面的具体问题仔细观察这个媒体并回答；"
                  "如果其中确实看不到相关信息，请明确说「看不到」，不要凭常理猜测。\n"
                  f"问题：{question}")
        extra = [{"type": "input_audio", "input_audio": audio_ref}] if audio_ref else None
        return await self._chat_with_media(
            ref, ct, model, prompt, max_tokens=1500,
            timeout=self._ASK_TIMEOUT.get(kind, 120), extra_parts=extra)

    def file_exists(self, file_id: str) -> bool:
        """校验远端 file_id 是否仍有效（200=有效，404/异常=失效）。"""
        if not self.api_key or not file_id:
            return False
        try:
            self._get_json(f"files/{file_id}", timeout=15)
            return True
        except Exception:  # noqa: BLE001
            return False

    # ----------------------------- 文生图 -----------------------------
    async def generate_image(self, prompt: str) -> str:
        if not self.openai_ready:
            raise ServiceUnavailable("未配置 api_key，无法生成图片")
        try:
            resp = await asyncio.to_thread(
                self._post_json, "images/generations",
                {"model": self.image_model, "prompt": prompt,
                 "size": self.image_size, "n": 1, "response_format": "b64_json",
                 "watermark": self.image_watermark},
            )
            item = (resp.get("data") or [{}])[0]
            if item.get("b64_json"):
                data = base64.b64decode(item["b64_json"])
            elif item.get("url"):
                # 兼容只返回 url 的端点（如豆包 images/generations）
                data = await asyncio.to_thread(self._download_bytes, item["url"])
            else:
                raise ServiceUnavailable("文生图未返回 b64_json 或 url")
            out = self.cache_dir / f"img_{uuid.uuid4().hex}.png"
            out.write_bytes(data)
            return str(out)
        except ServiceUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ServiceUnavailable(f"文生图失败: {exc}")

    # ----------------------------- TTS（豆包 openspeech V3 create） -----------------------------
    async def text_to_speech(self, text: str) -> str:
        """豆包语音合成 V3（openspeech /api/v3/tts/create，模型 seed-audio-1.0）。

        鉴权：X-Api-Key + X-Api-Request-Id（该接口无需 X-Api-Resource-Id）。
        请求体为 model / text_prompt / audio_config / watermark。
        响应为单个 JSON：audio(或 data) 字段为 base64 音频。
        """
        text = (text or "").strip()
        if not text:
            raise ServiceUnavailable("TTS 输入为空")
        if not self.voice_api_key:
            raise ServiceUnavailable("未配置 voice_api_key，无法合成语音")
        reqid = uuid.uuid4().hex
        payload = {
            "model": self.tts_model,
            "text_prompt": text,
            "audio_config": {
                "format": "mp3",
                "sample_rate": 24000,
                "pitch_rate": 0,
                "speech_rate": 0,
                "loudness_rate": 0,
            },
            "watermark": {},
        }
        headers = {
            "Content-Type": "application/json",
            "X-Api-Key": self.voice_api_key,
            "X-Api-Request-Id": reqid,
        }
        try:
            obj = await asyncio.to_thread(
                self._post_json_raw, self.tts_url, payload, headers)
            if isinstance(obj, str):
                obj = json.loads(obj)
            code = obj.get("code")
            if code not in (None, 0, "0"):
                raise ServiceUnavailable(
                    f"语音合成失败: code={code} msg={obj.get('message')}")
            audio_b64 = obj.get("audio") or obj.get("data")
            if not audio_b64:
                raise ServiceUnavailable("语音合成未返回音频数据")
            audio = base64.b64decode(audio_b64)
            out = self.cache_dir / f"tts_{uuid.uuid4().hex}.mp3"
            out.write_bytes(audio)
            return str(out)
        except ServiceUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ServiceUnavailable(f"语音合成失败: {exc}")

    # ----------------------------- ASR（豆包 openspeech 录音文件识别 bigmodel V2） -----------------------------
    def _post_json_resp(self, url: str, payload: dict, headers: dict, timeout: float = 120):
        """POST JSON 并返回 (status_code, header_dict, body_dict)，用于需要读取响应头的接口。"""
        if not url:
            raise ServiceUnavailable("未配置语音服务地址")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with _urlopen(req, timeout=timeout) as resp:
                status = getattr(resp, "status", resp.getcode())
                hdrs = dict(resp.getheaders())
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "ignore")
            except Exception:  # noqa: BLE001
                pass
            raise ServiceUnavailable(f"HTTP {e.code}: {e.reason} | body={detail[:300]}")
        try:
            body_obj = json.loads(raw.decode("utf-8", "ignore"))
        except Exception:  # noqa: BLE001
            body_obj = {}
        return status, hdrs, body_obj

    def _to_mp3(self, path: Path) -> bytes:
        """用 ffmpeg 把任意音频转成 mp3 字节（录音文件识别接受 mp3；QQ 语音多为 silk/mp3，统一转 mp3 最稳）。"""
        ffmpeg = _ffmpeg_exe("ffmpeg")
        if not ffmpeg:
            raise ServiceUnavailable(
                "语音识别需要 ffmpeg 将音频转码为 mp3（火山文件识别要求）。"
                "请在本机安装 ffmpeg 并加入 PATH 后重试。"
            )
        try:
            proc = subprocess.run(
                [ffmpeg, "-y", "-i", str(path), "-f", "mp3", "-ac", "1",
                 "-ar", "16000", "pipe:1"],
                check=True, capture_output=True, timeout=self.ffmpeg_timeout,
            )
            return proc.stdout
        except subprocess.CalledProcessError as e:  # pragma: no cover
            raise ServiceUnavailable(f"ffmpeg 转码失败: {e.stderr.decode('utf-8', 'ignore')[:200]}")
        except subprocess.TimeoutExpired:  # pragma: no cover
            raise ServiceUnavailable("ffmpeg 转码超时")

    def _has_audio_stream(self, path: Path) -> bool:
        """用 ffprobe 判断文件是否含音轨（避免对无声视频转码，ffmpeg 会直接报错）。"""
        ffprobe = _ffmpeg_exe("ffprobe")
        if not ffprobe:
            return True
        try:
            proc = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "a",
                 "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
                check=True, capture_output=True, timeout=30)
            return bool(proc.stdout.strip())
        except Exception:  # noqa: BLE001
            return False

    async def _extract_audio_for_video(self, video: Path):
        """从视频抽音轨并上传，返回 (audio_ref | None, 临时文件 | None)。

        为什么必须单独抽：全模态模型**不会**自动解析 video_url 里的音轨。
        实测三组对照（同一段带 880Hz 音轨的视频）：
          A. video_url + 抽帧预处理     → "没有任何声音"
          B. video_url 不抽帧           → "未听到任何音频"
          C. video_url + 独立 input_audio → "持续的高频电子蜂鸣测试音" ✅
        所以要让模型"听到"，只能把音轨作为独立的 audio 模态再传一份。
        视频无音轨 / 无 ffmpeg / 抽帧失败时返回 (None, None)，退化为纯画面分析。
        """
        if not _ffmpeg_exe("ffmpeg"):
            return None, None
        tmp = None
        try:
            if not await asyncio.to_thread(self._has_audio_stream, video):
                logger.info("视频 %s 无音轨，按纯画面分析", video.name)
                return None, None
            mp3_bytes = await asyncio.to_thread(self._to_mp3, video)
            if not mp3_bytes:
                return None, None
            tmp = video.parent / f"_vaudio_{uuid.uuid4().hex}.mp3"
            tmp.write_bytes(mp3_bytes)
            ref = await self._ingest_media(str(tmp), content_type="input_audio")
        except Exception as exc:  # noqa: BLE001
            logger.warning("视频音轨提取失败（仅分析画面）: %s", exc)
            if tmp is not None:
                tmp.unlink(missing_ok=True)
            return None, None
        logger.info("视频音轨已提取：%s（mp3 %d KB）", video.name, len(mp3_bytes) // 1024)
        return ref, tmp

    # ----------------------------- ASR 辅助（时长探测 / 长音频切分） -----------------------------
    def _probe_duration(self, path: Path) -> float | None:
        """用 ffprobe 取音频时长（秒），失败/缺失返回 None。"""
        ffprobe = _ffmpeg_exe("ffprobe")
        if not ffprobe:
            return None
        try:
            proc = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", str(path)],
                check=True, capture_output=True, timeout=30,
            )
            val = proc.stdout.decode("utf-8", "ignore").strip()
            return float(val) if val else None
        except Exception:  # noqa: BLE001
            return None

    def _split_mp3(self, mp3_path: Path, segment_seconds: int) -> list[Path]:
        """用 ffmpeg segment muxer 把 mp3 切成不超过 segment_seconds 的若干段。

        重新编码（而非 -c copy）以保证切点在帧边界，避免段首/段尾爆音或解码失败。
        """
        ffmpeg = _ffmpeg_exe("ffmpeg")
        if not ffmpeg:
            raise ServiceUnavailable("语音识别需要 ffmpeg 进行长音频切分，请先安装并加入 PATH。")
        out_dir = self.cache_dir / f"asr_seg_{uuid.uuid4().hex}"
        out_dir.mkdir(parents=True, exist_ok=True)
        pattern = out_dir / "seg_%03d.mp3"
        try:
            subprocess.run(
                [ffmpeg, "-y", "-i", str(mp3_path),
                 "-f", "segment", "-segment_time", str(segment_seconds),
                 "-reset_timestamps", "1", "-ac", "1", "-ar", "16000",
                 str(pattern)],
                check=True, capture_output=True, timeout=self.ffmpeg_timeout,
            )
        except subprocess.CalledProcessError as e:  # pragma: no cover
            raise ServiceUnavailable(
                f"ffmpeg 切分失败: {e.stderr.decode('utf-8', 'ignore')[:200]}")
        except subprocess.TimeoutExpired:  # pragma: no cover
            raise ServiceUnavailable("ffmpeg 切分超时")
        segs = sorted(out_dir.glob("seg_*.mp3"))
        if not segs:
            raise ServiceUnavailable("长音频切分未产生任何片段")
        return segs

    def _transcribe_segment(self, b64: str) -> str:
        """提交单段 base64 音频（submit）+ 轮询（query）返回识别文本。"""
        task_id = uuid.uuid4().hex
        common_headers = {
            "Content-Type": "application/json",
            "X-Api-Key": self.voice_api_key,
            "X-Api-Resource-Id": self.asr_resource_id,
            "X-Api-Request-Id": task_id,
        }
        submit_body = {
            "audio": {"data": b64, "format": "mp3"},
            "request": {
                "enable_channel_split": True,
                "enable_ddc": True,
                "enable_speaker_info": True,
                "enable_punc": True,
                "enable_itn": True,
            },
        }
        # 1) 提交任务（以响应头 X-Api-Status-Code=20000000 为成功标志）
        _, sub_hdrs, sub_body = self._post_json_resp(
            self.asr_submit_url, submit_body, common_headers)
        sub_code = sub_hdrs.get("X-Api-Status-Code") or str(sub_body.get("code", ""))
        if sub_code and sub_code not in ("20000000",) and sub_body.get("code") not in (None, 0, "0"):
            raise ServiceUnavailable(
                f"语音识别提交失败: status={sub_code} msg={sub_hdrs.get('X-Api-Message')} {sub_body}")
        # 2) 轮询（result 出现即完成；静音 20000003 返回空文本）
        final_text = ""
        for _ in range(40):  # 最多约 80s
            time.sleep(2)
            _, qhdrs, qbody = self._post_json_resp(self.asr_query_url, {}, common_headers)
            if "result" in qbody and isinstance(qbody.get("result"), dict):
                final_text = (qbody["result"].get("text") or "").strip()
                break
            code = qbody.get("code")
            msg = str(qbody.get("message", ""))
            if str(code) in ("20000003",):
                break
            if code not in (None, 0, "0", "20000000") and "OK" not in msg:
                raise ServiceUnavailable(f"语音识别查询失败: code={code} msg={msg}")
        return final_text.strip()

    async def speech_to_text(self, audio_path: str) -> str:
        """录音文件识别（openspeech /api/v3/auc/bigmodel/submit + /query）。

        流程：本地音频转 mp3 → 过长时按固定时长切分为多段（每段内联 base64 提交）
        → 逐段提交任务并轮询 → 拼接各段文本。
        说明：openspeech **没有独立文件上传端点**（实测 /upload 等全 404），
        audio 仅支持 data(内联) 或 url(公网)。本方法针对「本地长音频」用 ffmpeg
        切分，避免单请求体积超限，且无需任何外部托管。
        鉴权：X-Api-Key + X-Api-Resource-Id(volc.seedasr.auc) + X-Api-Request-Id。
        """
        if not self.voice_api_key:
            raise ServiceUnavailable("未配置 voice_api_key，无法做语音识别（openspeech 需语音专用 Key）")
        p = Path(audio_path)
        if not p.exists():
            raise ServiceUnavailable(f"语音文件不存在: {audio_path}")
        # 1) 统一转码为 mp3
        try:
            audio_bytes = await asyncio.to_thread(self._to_mp3, p)
        except ServiceUnavailable:
            raise

        # 2) 长音频切分：时长 > CHUNK_SECONDS 或体量过大时切分（内联 base64 有单请求上限）
        CHUNK_SECONDS = 600   # 单段最多 10 分钟
        CHUNK_BYTES = 25 * 1024 * 1024  # 单段最多约 25MB
        mp3_path = self.cache_dir / f"asr_in_{uuid.uuid4().hex}.mp3"
        mp3_path.write_bytes(audio_bytes)
        dur = self._probe_duration(mp3_path)
        need_split = (dur and dur > CHUNK_SECONDS) or len(audio_bytes) > CHUNK_BYTES
        if need_split:
            try:
                seg_paths = await asyncio.to_thread(self._split_mp3, mp3_path, CHUNK_SECONDS)
            except ServiceUnavailable:
                # 切分失败则退回整段提交（靠 API 自身限制兜底，错误会如实抛出）
                seg_paths = [mp3_path]
        else:
            seg_paths = [mp3_path]

        # 3) 逐段识别并拼接
        texts = []
        try:
            for seg in seg_paths:
                b = seg.read_bytes()
                b64 = base64.b64encode(b).decode("ascii")
                t = await asyncio.to_thread(self._transcribe_segment, b64)
                if t:
                    texts.append(t)
            return "\n".join(texts).strip()
        except ServiceUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ServiceUnavailable(f"语音识别失败: {exc}")

    # ----------------------------- 联网搜索 -----------------------------
    def _deepseek_search_sync(self, query: str, max_results: int = 5) -> str | None:
        """DeepSeek 自研搜索 API：POST {base}/v1/tools/search。

        为什么优先用它：官方接口、结构化返回（带相关度/权威性评分）、实测 0.3 秒级响应，
        不依赖搜索引擎的页面结构——不会像抓 HTML 那样因改版或反爬而整体失效。

        返回格式化文本；**不可用或无结果时返回 None**，由 `web_search` 回退到原有引擎链。
        任何异常都在这里吞掉，绝不波及保底链路。
        """
        if not self.ds_search_enable or not self.ds_api_key or not query:
            return None
        count = max(1, min(50, max_results or self.ds_search_count))
        body: dict = {"query": query[:128], "count": count}
        if self.ds_search_content:
            body["need_content"] = True
        try:
            resp = self._post_json_raw(
                f"{self.ds_base_url}/v1/tools/search", body,
                {"Content-Type": "application/json",
                 "Authorization": f"Bearer {self.ds_api_key}"},
                timeout=self.ds_search_timeout)
        except Exception as exc:  # noqa: BLE001
            logger.warning("DeepSeek 搜索不可用，回退搜索引擎: %s", exc)
            return None
        items = (resp or {}).get("results") or []
        if not items:
            return None
        lines = []
        for i, it in enumerate(items[:count], 1):
            title = str(it.get("title") or "").strip()
            link = str(it.get("url") or "").strip()
            summary = str(it.get("summary") or it.get("snippet") or "").strip()
            block = f"{i}. {title}".rstrip()
            if summary:
                block += f"\n   {summary}"
            if link:
                block += f"\n   {link}"
            lines.append(block)
        return "\n".join(lines) if lines else None

    async def web_search(self, query: str, max_results: int = 5) -> str:
        """联网搜索。顺序：DeepSeek 官方搜索 → 引擎链（Bing → DuckDuckGo → 百度 → 搜狗）保底。

        引擎链**必须保留**：DeepSeek 搜索可能因未配 key、余额不足(402)、限流(429)或
        网络不通而失败，那时抓搜索引擎 HTML 仍是唯一能出结果的路子。
        两段的网络请求都不在事件循环里跑（都用 asyncio.to_thread 包了）。
        """
        # ① 首选：DeepSeek 官方搜索
        tried = []
        if self.ds_search_enable and self.ds_api_key:
            tried.append("DeepSeek(官方)")
            try:
                got = await asyncio.to_thread(
                    self._deepseek_search_sync, query, max_results)
                if got:
                    return got
            except Exception as exc:  # noqa: BLE001
                logger.warning("DeepSeek 搜索异常，回退引擎链: %s", exc)

        # ② 保底引擎链（同步，丢进线程跑，避免阻塞事件循环）
        return await asyncio.to_thread(
            self._search_engines_sync, query, max_results, tried)

    def _search_engines_sync(self, query: str, max_results: int,
                             tried: list | None = None) -> str:
        """保底引擎链（Bing → DuckDuckGo → 百度 → 搜狗），**同步执行**。

        单独拆成同步函数是为了用 asyncio.to_thread 包起来：四个引擎逐个尝试、
        每个最长 20 秒，直接在 async 函数里跑会阻塞事件循环最多 80 秒。

        引擎优先级与原因：
          - Bing      ：国内稳定可访问，作为主引擎（实测可靠性最高）。
          - DuckDuckGo：国际引擎，部分网络不可达（握手超时），作二级兜底。
          - 百度/搜狗 ：国内可达，但对裸请求常返回反爬验证页（解析为空），
                        作三级/四级兜底——只要任一返回真实结果即用，
                        全部失败才给出友好提示，绝不抛异常拖垮 bot。
        """
        _BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36")

        # 各引擎：构建请求 -> 解析结果 的声明式列表（按顺序尝试）
        def _get(url: str) -> str:
            req = urllib.request.Request(url, method="GET")
            req.add_header("User-Agent", _BROWSER_UA)
            req.add_header(
                "Accept",
                "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
            req.add_header("Accept-Language", "zh-CN,zh;q=0.9")
            with _urlopen(req, timeout=20) as resp:
                return resp.read().decode("utf-8", errors="ignore")

        engines = [
            ("Bing",
             "https://www.bing.com/search?" + urllib.parse.urlencode(
                 {"q": query, "setlang": "zh-CN", "cc": "CN"}),
             _parse_bing_results),
            ("DuckDuckGo",
             "https://lite.duckduckgo.com/lite/",
             None),  # DDG 用 POST + 专用解析，下面特判
            ("百度",
             "https://www.baidu.com/s?" + urllib.parse.urlencode(
                 {"wd": query, "rn": str(max_results)}),
             _parse_baidu_results),
            ("搜狗",
             "https://www.sogou.com/web?" + urllib.parse.urlencode(
                 {"query": query}),
             _parse_sogou_results),
        ]

        # ② 保底：原有引擎链（Bing → DuckDuckGo → 百度 → 搜狗），任一返回结果即用
        failed = list(tried)
        for name, url, parser in engines:
            try:
                if name == "DuckDuckGo":
                    # DDG lite 为 POST 表单
                    form = urllib.parse.urlencode({"q": query}).encode("utf-8")
                    req = urllib.request.Request(url, data=form, method="POST")
                    req.add_header("Content-Type", "application/x-www-form-urlencoded")
                    req.add_header("User-Agent", _BROWSER_UA)
                    with _urlopen(req, timeout=20) as resp:
                        html = resp.read().decode("utf-8", errors="ignore")
                    titles = re.findall(r'class="result-link"[^>]*>(.*?)</a>', html, re.S)
                    snippets = re.findall(r'class="result-snippet"[^>]*>(.*?)</td>', html, re.S)
                    clean = lambda s: re.sub(r"<[^>]+>", "", s).strip()
                    lines = []
                    for i in range(min(max_results, max(len(titles), len(snippets)))):
                        t = clean(titles[i]) if i < len(titles) else ""
                        s = clean(snippets[i]) if i < len(snippets) else ""
                        if t or s:
                            lines.append(f"{i+1}. {t}\n   {s}")
                    if lines:
                        return "\n".join(lines)
                else:
                    html = _get(url)
                    results = parser(html, max_results) if parser else []
                    if results:
                        return "\n".join(results)
                failed.append(name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s 搜索失败，尝试下一个引擎: %s", name, exc)
                failed.append(name)

        return (f"（联网搜索失败：已依次尝试 {', '.join(failed) or '全部'} 均未返回结果，"
                f"可能是当前网络受限、触发了反爬验证或搜索服务不可用。请检查网络连接或稍后重试。）")

    # ----------------------------- 打开链接（抓取网页内容） -----------------------------
    def _fetch_url_sync(self, url: str, max_chars: int,
                        web_cfg: dict | None) -> tuple[str, bool]:
        """阶段 1（同步）：B 站官方 API → urllib 下载 + 剥标签。

        返回 (text, is_html)；只有 is_html 为真时调用方才需要考虑无头浏览器渲染。
        为什么单独拆成同步函数：直接在 async 函数里跑 urllib 会把**整个事件循环**
        阻塞住（单次最长 20 秒），期间 WebSocket 读循环停摆、所有等待中的
        OneBot API 响应都会撞满超时。用 asyncio.to_thread 包起来就不会。
        """
        # B 站 / b23.tv 短链：优先用官方 API 取结构化元数据（视频页是 SPA，抓 HTML 无正文）
        try:
            bvid = Services._resolve_bilibili_bvid(url)
            if bvid:
                info = Services._fetch_bilibili(bvid, max_chars)
                if info:
                    return info, False
        except Exception as exc:  # noqa: BLE001
            logger.warning("B站链接解析失败，退回通用抓取: %s", exc)

        is_html = False
        try:
            req = urllib.request.Request(url, method="GET")
            req.add_header("User-Agent", _UA)
            req.add_header(
                "Accept",
                "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
            with _urlopen(req, timeout=20) as resp:
                ctype = (resp.headers.get("Content-Type") or "").lower()
                enc = (resp.headers.get("Content-Encoding") or "").lower()
                # 无扩展名但服务器标了二进制媒体（常见于 CDN 直链）：按文本解码只会得到乱码
                if ctype.startswith(("image/", "audio/", "video/")):
                    kind_cn = {"image": "图片", "audio": "音频", "video": "视频"}[
                        ctype.split("/")[0]]
                    return ((f"（该链接直接指向{kind_cn}文件，Content-Type={ctype}，不是网页；"
                             f"已跳过文本解析以免返回乱码）"), False)
                # 文档（PDF/Office）同理：按文本解码只会得到乱码
                if _is_doc_mime(ctype):
                    return ((f"（该链接直接指向文档文件，Content-Type={ctype}，不是网页；"
                             f"已跳过文本解析。需要用文档模型阅读请把该链接单独发我）"), False)
                raw = resp.read(2_000_000)  # 最多读 2MB，防止大二进制撑爆内存
            # 部分站点强制 gzip/br 压缩，urllib 不会自动解压，必须手动处理
            raw = _decompress(raw, enc)
            if "json" in ctype or url.lower().split("?")[0].endswith(".json"):
                text = raw.decode("utf-8", "ignore")
            else:
                charset = "utf-8"
                m = re.search(r"charset=([\w-]+)", ctype)
                if m:
                    charset = m.group(1)
                html = raw.decode(charset, "ignore")
                is_html = ("html" in ctype) or ("<html" in html.lower())
                text = _strip_html(html) if is_html else html
                if is_html:
                    # 供 fetch_page_media 复用，避免同一页面抓两遍
                    _cache_put_html(url, html, ctype)
            text = re.sub(r"\s+", " ", text).strip()
        except Exception as exc:  # noqa: BLE001
            return f"（无法打开链接 {url}：{exc}）", False
        return text, is_html

    async def fetch_url(self, url: str, max_chars: int = 3500,
                        web_cfg: dict | None = None) -> str:
        """抓取并读取一个网页/链接，返回纯文本内容（用于"打开链接看内容"）。

        两阶段策略：
          1) 传统方式：urllib 下载 HTML + 剥标签（快、轻，对服务端渲染 SSR 站点有效）。
             ——同步部分，用 asyncio.to_thread 包起来，不阻塞事件循环。
          2) 若传统方式剥离后文字过少（疑似 React/Vue 等 SPA 空壳），且开启了
             无头浏览器，则启动 Chromium 执行 JS 渲染再抽取可见文本（通用覆盖任意 SPA）。
             ——Playwright 用的是 async API，必须留在事件循环里，不能进线程。
        B 站 / b23.tv 走专属官方 API（视频页也是 SPA，但 API 更稳定精确）。
        任何环节失败都给出友好提示，不抛出（避免 bot 因单个坏链接崩溃）。
        """
        cfg = web_cfg or {}
        headless = bool(cfg.get("headless_browser", True))
        threshold = int(cfg.get("spa_text_threshold", 200))
        browser_timeout = int(cfg.get("browser_timeout", 20))
        wait_until = cfg.get("wait_until", "load")
        render_wait_ms = int(cfg.get("render_wait_ms", 1500))

        text, is_html = await asyncio.to_thread(
            self._fetch_url_sync, url, max_chars, web_cfg)

        # 阶段 2：疑似 SPA（传统抓取文字过少）→ 无头浏览器执行 JS 渲染
        if headless and is_html and _HAVE_PLAYWRIGHT and len(text) < threshold:
            try:
                rendered = await Services._fetch_with_browser(
                    url, max_chars, browser_timeout, wait_until, render_wait_ms)
                if rendered and len(rendered) > len(text):
                    text = rendered
            except Exception as exc:  # noqa: BLE001
                logger.warning("无头浏览器渲染失败，退回传统抓取结果: %s", exc)

        if not text:
            return f"（链接 {url} 未解析到可读文本）"
        if len(text) > max_chars:
            text = text[:max_chars] + " …(内容已截断)"
        return text

    @staticmethod
    async def _fetch_with_browser(url: str, max_chars: int, timeout: int,
                                  wait_until: str, render_wait_ms: int) -> str | None:
        """用 Playwright 启动无头 Chromium，执行 JS 渲染后抽取页面可见文本。

        专治 React/Vue 等 SPA：静态 HTML 是空壳，必须跑 JS 才有正文。
        浏览器内核缺失或启动失败时抛异常，由调用方降级到传统抓取结果。
        """
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
            try:
                page = await browser.new_page(user_agent=_UA)
                await page.goto(url, timeout=timeout * 1000, wait_until=wait_until)
                # load 之后再额外等待，让 SPA 异步数据渲染完成
                if render_wait_ms > 0:
                    try:
                        await page.wait_for_timeout(render_wait_ms)
                    except Exception:  # noqa: BLE001
                        pass
                # 优先取可见文本（innerText 比剥 HTML 更贴近人眼所见）
                content = await page.evaluate(
                    "() => (document.body ? document.body.innerText : "
                    "document.documentElement.innerText) || ''")
            finally:
                await browser.close()
        text = re.sub(r"\s+", " ", (content or "")).strip()
        if not text:
            return None
        if len(text) > max_chars:
            text = text[:max_chars] + " …(内容已截断)"
        return text

    # --------------------- 网页内嵌媒体：提取 / 渲染取 / 下载 ---------------------
    @staticmethod
    def _fetch_page_media_sync(url: str) -> tuple[dict, bool]:
        """阶段 1（同步）：静态 HTML → 解析内嵌媒体。供 fetch_page_media 用 to_thread 调用。

        返回 (found, may_render)：二进制直链（本身不是网页）时 may_render 为 False，
        调用方据此跳过无头浏览器——那类链接根本不存在"内嵌资源"。
        """
        empty = {"title": "", "videos": [], "audios": [], "images": [],
                 "embeds": [], "docs": []}
        raw_html = ""
        cached = _cache_get_html(url)
        if cached:
            raw_html = cached[0]
        else:
            try:
                req = urllib.request.Request(url, method="GET")
                req.add_header("User-Agent", _UA)
                req.add_header("Accept", "text/html,application/xhtml+xml,*/*;q=0.8")
                with _urlopen(req, timeout=20) as resp:
                    ctype = (resp.headers.get("Content-Type") or "").lower()
                    enc = (resp.headers.get("Content-Encoding") or "").lower()
                    # 本身就是二进制媒体（而非网页）→ 没有"内嵌资源"可提取
                    if ctype.startswith(("image/", "audio/", "video/")):
                        return empty, False
                    raw = resp.read(3_000_000)  # 3MB 上限，够覆盖绝大多数页面的 head/body
                raw = _decompress(raw, enc)
                charset = "utf-8"
                m = re.search(r"charset=([\w-]+)", ctype)
                if m:
                    charset = m.group(1)
                raw_html = raw.decode(charset, "ignore")
                _cache_put_html(url, raw_html, ctype)
            except Exception as exc:  # noqa: BLE001
                logger.warning("抓取页面（媒体）失败 %s: %s", url, exc)

        if not raw_html:
            return empty, True
        p = _PageMediaParser()
        try:
            p.feed(raw_html)
        except Exception as exc:  # noqa: BLE001
            logger.warning("解析页面媒体标签失败 %s: %s", url, exc)
        found = {"title": p.title or "", "videos": p.videos, "audios": p.audios,
                 "images": p.images, "embeds": p.embeds, "docs": p.docs}
        return found, True

    async def fetch_page_media(self, url: str, web_cfg: dict | None = None,
                               max_each: int = 3) -> dict:
        """抓取网页并提取其中内嵌的媒体资源（视频/音频/图片/平台播放器）。

        返回 {"title", "videos", "audios", "images", "embeds"}，元素均为**绝对 URL**；
        找不到就给空列表。两阶段：
          1) 抓静态 HTML，用 _PageMediaParser 解析 og 元信息 / video / audio / source / iframe；
          2) 若一个媒体都没发现（典型 SPA），且开了无头浏览器，则渲染后从 DOM 取。
        任何失败都返回空结果而不抛异常（与 fetch_url 相同的降级策略）。
        """
        cfg = web_cfg or {}
        headless = bool(cfg.get("headless_browser", True))
        browser_timeout = int(cfg.get("browser_timeout", 20))
        wait_until = cfg.get("wait_until", "load")
        render_wait_ms = int(cfg.get("render_wait_ms", 1500))

        # 阶段 1：静态 HTML（优先复用 fetch_url 已抓过的缓存，同一页面不重复下载）
        #         同步抓取，用 to_thread 包起来，避免阻塞事件循环（单次最长 20 秒）
        found, may_render = await asyncio.to_thread(
            Services._fetch_page_media_sync, url)

        # 阶段 2：静态 HTML 里一个媒体都没有 → 可能是 JS 注入，交给无头浏览器
        if (may_render and not (found["videos"] or found["audios"] or found["embeds"])
                and headless and _HAVE_PLAYWRIGHT):
            try:
                dom = await Services._browser_media(
                    url, browser_timeout, wait_until, render_wait_ms)
                if dom:
                    for k in ("videos", "audios", "images", "embeds", "docs"):
                        for it in dom.get(k) or []:
                            if it not in found[k]:
                                found[k].append(it)
                    if not found["title"]:
                        found["title"] = dom.get("title") or ""
            except Exception as exc:  # noqa: BLE001
                logger.warning("无头浏览器提取媒体失败 %s: %s", url, exc)

        # 相对 URL → 绝对；iframe 只留主流平台；按类别限量
        out = {"title": found["title"], "videos": [], "audios": [],
               "images": [], "embeds": [], "docs": []}
        for key in ("videos", "audios", "images", "embeds", "docs"):
            for item in found[key]:
                absu = urllib.parse.urljoin(url, (item or "").strip())
                if not absu.startswith(("http://", "https://")):
                    continue
                if key == "embeds" and not _is_platform_video(absu):
                    continue
                if absu not in out[key]:
                    out[key].append(absu)
                if len(out[key]) >= max_each:
                    break
        return out

    @staticmethod
    async def _browser_media(url: str, timeout: int, wait_until: str,
                             render_wait_ms: int) -> dict | None:
        """渲染页面后从 DOM 提取媒体（覆盖 JS 动态插入的视频/音频/大图/播放器）。

        与 fetch_url 的浏览器阶段不同：这里不取正文，只取媒体地址。
        """
        from playwright.async_api import async_playwright
        js = """() => {
          const out = {title: document.title || '', videos: [], audios: [],
                       images: [], embeds: [], docs: []};
          const abs = (u) => { try { return new URL(u, location.href).href; } catch (e) { return ''; } };
          const push = (arr, u) => { if (u && !arr.includes(u)) arr.push(u); };
          const DOC_RE = /\\.(pdf|docx?|xlsx?|pptx?|rtf|odt|ods|odp|epub|csv)$/i;
          const isDoc = (u) => DOC_RE.test((u || '').split('?')[0].split('#')[0]);
          document.querySelectorAll('video').forEach(el => {
            push(out.videos, abs(el.currentSrc || el.src || el.getAttribute('src') || ''));
            const poster = el.getAttribute('poster');
            if (poster) push(out.images, abs(poster));
          });
          document.querySelectorAll('audio').forEach(el => {
            push(out.audios, abs(el.currentSrc || el.src || el.getAttribute('src') || ''));
          });
          document.querySelectorAll('source').forEach(el => {
            const t = (el.getAttribute('type') || '').toLowerCase();
            const u = abs(el.getAttribute('src') || '');
            if (t.startsWith('audio')) push(out.audios, u);
            else if (t.startsWith('video')) push(out.videos, u);
          });
          [...document.querySelectorAll('img')]
            .filter(el => el.naturalWidth >= 300 && el.naturalHeight >= 200)
            .slice(0, 3)
            .forEach(el => push(out.images, abs(el.currentSrc || el.src || '')));
          document.querySelectorAll('iframe').forEach(el => {
            const u = abs(el.getAttribute('src') || '');
            if (isDoc(u)) push(out.docs, u);   // 内嵌的 PDF 预览
            else push(out.embeds, u);
          });
          document.querySelectorAll('embed[src], object[data]').forEach(el => {
            const u = abs(el.getAttribute('src') || el.getAttribute('data') || '');
            if (isDoc(u)) push(out.docs, u);
          });
          [...document.querySelectorAll('a[href]')].slice(0, 400).forEach(el => {
            const u = abs(el.getAttribute('href') || '');
            if (isDoc(u) && out.docs.length < 4) push(out.docs, u);
          });
          return out;
        }"""
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
            try:
                page = await browser.new_page(user_agent=_UA)
                await page.goto(url, timeout=timeout * 1000, wait_until=wait_until)
                if render_wait_ms > 0:
                    try:
                        await page.wait_for_timeout(render_wait_ms)
                    except Exception:  # noqa: BLE001
                        pass
                data = await page.evaluate(js)
            finally:
                await browser.close()
        return data if isinstance(data, dict) else None

    @staticmethod
    def download_media(url: str, max_seconds: int = 180, max_mb: int = 40,
                       out_dir: str | None = None) -> tuple[str, dict]:
        """用 yt-dlp 把网页/平台上的视频下载到本地，供视频模型"看"。

        先用 skip_download 读时长做门禁（避免白下载长视频），再下小分辨率 mp4
        （height<=480，体积小、模型仍能看清细节，合并依赖 ffmpeg）。
        返回 (本地路径, {"title","duration","uploader","webpage_url"})。
        未装 yt-dlp / 超时长 / 超体积 / 下载失败 → 抛 ServiceUnavailable。
        注意：这是同步阻塞调用，异步环境请用 asyncio.to_thread 包装。
        """
        try:
            import yt_dlp
        except ImportError:
            raise ServiceUnavailable(
                "未安装 yt-dlp，无法下载网页视频（在项目 venv 执行 pip install yt-dlp）")

        workdir = out_dir or tempfile.mkdtemp(prefix="qqbot-media-")
        common = {
            "quiet": True, "no_warnings": True, "noplaylist": True,
            "socket_timeout": 25, "retries": 2,
            "outtmpl": str(Path(workdir) / "%(id)s.%(ext)s"),
        }
        # 机房 IP 访问 B 站易被 412 风控，有 Cookie 就带给 yt-dlp
        if _BILI_COOKIE and ("bilibili.com" in url or "b23.tv" in url):
            common["http_headers"] = {
                "User-Agent": _UA,
                "Referer": "https://www.bilibili.com/",
                "Cookie": _BILI_COOKIE,
            }
        try:
            with yt_dlp.YoutubeDL({**common, "skip_download": True}) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:  # noqa: BLE001
            raise ServiceUnavailable(f"解析视频页面失败: {str(exc)[:200]}")

        if isinstance(info, dict) and info.get("entries"):
            info = next((e for e in info["entries"] if e), {}) or {}
        duration = float(info.get("duration") or 0)
        if duration and duration > max_seconds:
            raise ServiceUnavailable(
                f"视频时长约 {duration:.0f}s，超过上限 {max_seconds}s，已跳过"
                f"（可调大 config.json 的 web.media_download_max_seconds）")

        ff_dir = _find_ffmpeg_dir()
        if ff_dir:
            # 有 ffmpeg：优先 DASH 的 bv+ba 分离流，合并成 mp4（画质/体积可控）
            fmt = ("bv*[ext=mp4][height<=480]+ba[ext=m4a]/"
                   "b[ext=mp4][height<=480]/b[ext=mp4]/bv*+ba/b")
        else:
            # 没有 ffmpeg 就无法合并音视频轨，只能挑现成的单文件格式
            logger.warning("未找到 ffmpeg，yt-dlp 只能下载单文件格式（画质可能较低）")
            fmt = "b[ext=mp4][height<=480]/b[ext=mp4]/b"
        opts = {
            **common,
            "format": fmt,
            "max_filesize": int(max_mb) * 1024 * 1024,
        }
        if ff_dir:
            opts["ffmpeg_location"] = ff_dir
            opts["merge_output_format"] = "mp4"
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info2 = ydl.extract_info(url, download=True) or {}
        except Exception as exc:  # noqa: BLE001
            raise ServiceUnavailable(f"视频下载失败: {str(exc)[:200]}")

        path = None
        for d in (info2.get("requested_downloads") or []):
            if d.get("filepath"):
                path = d["filepath"]
                break
        if path and not Path(path).exists():
            path = None
        if not path:
            cands = [p for p in Path(workdir).glob("*") if p.is_file()]
            if cands:
                newest = max(cands, key=lambda p: p.stat().st_mtime)
                path = str(newest)
        if not path:
            raise ServiceUnavailable("视频下载完成但未找到输出文件")

        meta = {
            "title": info2.get("title") or info.get("title") or "",
            "duration": duration or float(info2.get("duration") or 0),
            "uploader": info2.get("uploader") or "",
            "webpage_url": info2.get("webpage_url") or url,
        }
        return path, meta

    @staticmethod
    def probe_content_type(url: str, timeout: float = 6.0) -> str:
        """用 HEAD 探测 URL 的 Content-Type（失败返回空串）。

        用于覆盖"路径里没有扩展名"的直链，如 `/download?id=123`、`/file/9f3a`。
        服务器不支持 HEAD 或网络异常时返回空串，调用方按原逻辑继续。
        """
        for method in ("HEAD", "GET"):
            try:
                req = urllib.request.Request(url, method=method)
                req.add_header("User-Agent", _UA)
                with _urlopen(req, timeout=timeout) as resp:
                    ctype = (resp.headers.get("Content-Type") or "").lower()
                    if ctype:
                        return ctype
            except Exception:  # noqa: BLE001
                continue
        return ""

    @staticmethod
    def download_file(url: str, max_mb: int = 30) -> str:
        """把任意文件下载到临时路径，返回本地路径。

        主要用途：有些站点的文档直链带防盗链（要 UA/Referer）或有重定向，
        直接把 URL 交给方舟会拉取失败；先在本机落地，再走 Files API 上传更可靠。
        超过 max_mb 或下载失败抛 ServiceUnavailable。临时目录前缀 qqbot-doc-，
        由 agent 的清理函数统一删除。
        """
        limit = int(max_mb) * 1024 * 1024
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", _UA)
        try:
            with _urlopen(req, timeout=60) as resp:
                ctype = (resp.headers.get("Content-Type") or "").lower()
                data = resp.read(limit + 1)
        except Exception as exc:  # noqa: BLE001
            raise ServiceUnavailable(f"下载文件失败: {exc}")
        if len(data) > limit:
            raise ServiceUnavailable(f"文件超过 {max_mb}MB，已跳过")
        if not data:
            raise ServiceUnavailable("下载到的文件为空")
        ext = (_guess_ext_from_ctype(ctype)
               or Path(urllib.parse.urlparse(url).path).suffix
               or ".bin")
        d = Path(tempfile.mkdtemp(prefix="qqbot-doc-"))
        p = d / f"downloaded{ext}"
        p.write_bytes(data)
        return str(p)

    @staticmethod
    def _resolve_bilibili_bvid(url: str):
        """从 B 站链接或 b23.tv 短链解析出 BV 号；解析不到返回 None。"""
        m = re.search(r"BV[0-9A-Za-z]+", url)
        if m:
            return m.group(0)
        if "b23.tv" in url or "bilibili.com" in url:
            try:
                req = urllib.request.Request(url, method="GET")
                for k, v in _bili_headers().items():
                    req.add_header(k, v)
                with _urlopen(req, timeout=15) as r:
                    final = r.geturl()
                m = re.search(r"BV[0-9A-Za-z]+", final)
                if m:
                    return m.group(0)
            except Exception:  # noqa: BLE001
                return None
        return None

    @staticmethod
    def _fetch_bilibili(bvid: str, max_chars: int = 3500) -> str | None:
        """调用 B 站官方接口获取视频结构化元数据（标题/UP主/播放量/简介等）。

        视频页本身是 JS 动态渲染（SPA），抓 HTML 拿不到正文；官方 view 接口
        返回干净的 JSON，无需解压、无需渲染，稳定可用。
        """
        api = "https://api.bilibili.com/x/web-interface/view?bvid=" + bvid
        req = urllib.request.Request(api, method="GET")
        for k, v in _bili_headers().items():
            req.add_header(k, v)
        try:
            with _urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 412:
                logger.warning(
                    "B站接口返回 412（风控）。配置 web.bilibili_cookie（浏览器里的 SESSDATA）可缓解: %s", bvid)
            else:
                logger.warning("B站 API 请求失败 %s: %s", bvid, exc)
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("B站 API 请求失败 %s: %s", bvid, exc)
            return None
        if data.get("code") != 0:
            return None
        d = data.get("data", {})
        owner = d.get("owner", {})
        stat = d.get("stat", {})
        pub = d.get("pubdate")
        pub_s = datetime.datetime.fromtimestamp(pub).strftime("%Y-%m-%d") if pub else ""
        lines = [
            f"【B站视频】{d.get('title', '')}",
            f"UP主：{owner.get('name', '')}（UID {owner.get('mid', '')}）",
            f"播放 {stat.get('view', '')} · 点赞 {stat.get('like', '')} "
            f"· 弹幕 {stat.get('danmaku', '')} · 时长 {d.get('duration', '')}秒",
            (f"发布 {pub_s}" if pub_s else ""),
            f"简介：{d.get('desc', '')}",
        ]
        text = "\n".join(x for x in lines if x)
        return text if len(text) <= max_chars else text[:max_chars]
