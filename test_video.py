"""多模态理解离线测试（mock 网络，不真发请求）。

覆盖四类媒体 × 三种传入方式的"引用字段"是否正确（依方舟实测修正）：
  - image_url / video_url : 内联/直链用 url；Files API 上传用 file_id
  - file   (文档)         : 内联 file_data(+filename)；直链 file_url；上传 file_id
  - input_audio (音频)    : 内联 data+format；直链 url（不支持 file_id，本地不走上传）
并验证各走独立模型：视频=video_model，音频=audio_model，文档=document_model。
最后验证 agent._handle_file 的路由。
"""
import asyncio
import copy
import json
import re
import tempfile
import time
from pathlib import Path

from services import Services, ServiceUnavailable
import services as services_mod
from agent import Agent

CFG = {
    "services": {"openai": {
        "api_key": "ark-test",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "vision_model": "doubao-seed-evolving",
        "video_model": "doubao-seed-evolving",
        "video_fps": 0.3,
        "audio_model": "doubao-seed-2-1-lite-260915",
        "document_model": "doubao-seed-evolving",
        "image_model": "doubao-seedream-5-0-flash-260915",
        "tts_model": "seed-audio-1.0",
        "asr_model": "x",
        "voice_api_key": "v",
        "tts_url": "https://x",
        "asr_url": "https://x",
    }},
}

_MIME = {".mp4": "video/mp4", ".wav": "audio/wav",
         ".pdf": "application/pdf", ".png": "image/png"}


class FakeServices(Services):
    """把网络层换成记录/可控，避免真实请求。"""

    def __init__(self, cfg, upload="ok"):
        super().__init__(cfg)
        self.captured = None
        self.upload_mode = upload
        self.last_preprocess = None

    def _post_json(self, path, payload, timeout=60):
        self.captured = copy.deepcopy(payload)
        return {"choices": [{"message": {"content": "OK"}}]}

    def _upload_file(self, path, purpose="user_data", preprocess_configs=None):
        if self.upload_mode == "fail":
            raise ServiceUnavailable("forced upload fail")
        self.uploads = getattr(self, "uploads", 0) + 1
        self.last_preprocess = preprocess_configs
        return "file-fake-123"

    def _wait_file_processed(self, file_id, timeout_s=90):
        return {"status": "completed"}

    def file_exists(self, file_id):
        return bool(getattr(self, "file_exists_ok", True))

    def _delete_file(self, file_id):
        # 记录远端清理调用，避免测试发真实 DELETE 请求
        self.deleted = getattr(self, "deleted", []) + [file_id]

    def _guess_mime(self, path):
        return _MIME.get(Path(path).suffix.lower(), "application/octet-stream")


def _tmp(suffix: str) -> str:
    f = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    f.write(b"\x00" * 32)
    f.close()
    return f.name


def first_part(s) -> dict:
    return s.captured["messages"][0]["content"][0]


def test_video_fileid():
    s = FakeServices(CFG, upload="ok")
    p = _tmp(".mp4")
    asyncio.run(s.understand_video(p))
    part = first_part(s)
    assert part["type"] == "video_url", part
    assert part["video_url"].get("file_id") == "file-fake-123", part
    assert s.captured["model"] == "doubao-seed-evolving"
    assert s.last_preprocess and s.last_preprocess["video"]["model"] == "doubao-seed-evolving"
    # 默认 keep 策略：分析完保留 file_id（7 天自动过期），供多轮追问复用
    assert getattr(s, "deleted", []) == [], "keep 模式下不应删除远端文件"
    print("[OK] 视频 本地文件 → Files API file_id；预处理用 video_model；保留 file_id 供复用")


def test_video_base64_fallback():
    s = FakeServices(CFG, upload="fail")
    p = _tmp(".mp4")
    asyncio.run(s.understand_video(p))
    ref = first_part(s)["video_url"]
    assert ref["url"].startswith("data:video/mp4;base64,"), ref
    print("[OK] 视频 上传失败 → Base64 内联 video_url.url")


def test_video_url():
    s = FakeServices(CFG)
    asyncio.run(s.understand_video("https://x.com/a.mp4", is_url=True, fps=1))
    ref = first_part(s)["video_url"]
    assert ref["url"] == "https://x.com/a.mp4" and ref.get("fps") == 1, ref
    print("[OK] 视频 直链 → video_url.url + fps")


def test_audio_local_inputaudio():
    s = FakeServices(CFG)
    p = _tmp(".wav")
    asyncio.run(s.understand_audio(p))
    part = first_part(s)
    assert part["type"] == "input_audio", part
    # 现在优先走 Files API（实测 input_audio 支持 file_id），FakeServices 的上传桩返回该 id
    assert part["input_audio"].get("file_id") == "file-fake-123", part
    assert s.captured["model"] == "doubao-seed-2-1-lite-260915"
    print("[OK] 音频 本地 → input_audio{file_id}（优先 Files API）；模型 audio_model(lite)")


def test_audio_url_inputaudio():
    s = FakeServices(CFG)
    asyncio.run(s.understand_audio("https://x.com/a.wav", is_url=True))
    ref = first_part(s)["input_audio"]
    assert ref["url"] == "https://x.com/a.wav", ref
    assert s.captured["model"] == "doubao-seed-2-1-lite-260915"
    print("[OK] 音频 直链 → input_audio.url")


def test_document_fileid():
    s = FakeServices(CFG, upload="ok")
    p = _tmp(".pdf")
    asyncio.run(s.understand_document(p))
    part = first_part(s)
    assert part["type"] == "file", part
    assert part["file"].get("file_id") == "file-fake-123", part
    assert s.captured["model"] == "doubao-seed-evolving"
    print("[OK] 文档 本地 → Files API file_id；模型 document_model")


def test_document_filedata_fallback():
    s = FakeServices(CFG, upload="fail")
    p = _tmp(".pdf")
    asyncio.run(s.understand_document(p))
    ref = first_part(s)["file"]
    assert ref["file_data"].startswith("data:application/pdf;base64,"), ref
    assert ref.get("filename"), ref
    print("[OK] 文档 上传失败 → file_data(base64) + filename")


def test_document_fileurl():
    s = FakeServices(CFG)
    asyncio.run(s.understand_document("https://x.com/a.pdf", is_url=True))
    ref = first_part(s)["file"]
    assert ref["file_url"] == "https://x.com/a.pdf", ref
    print("[OK] 文档 直链 → file_url")


def test_image_fileid():
    s = FakeServices(CFG, upload="ok")
    p = _tmp(".png")
    asyncio.run(s.describe_image(p))
    part = first_part(s)
    assert part["type"] == "image_url", part
    assert part["image_url"].get("file_id") == "file-fake-123", part
    assert s.captured["model"] == "doubao-seed-evolving"
    print("[OK] 图片 本地 → Files API file_id；模型 vision_model")


class FakeLLM:
    system_prompt = "x"

    async def complete(self, *a, **k):
        return {}

    async def maybe_summarize(self, *a, **k):
        return None


def test_agent_routing():
    s = FakeServices(CFG, upload="ok")
    a = Agent(FakeLLM(), s)

    vp = _tmp(".mp4")
    asyncio.run(a._handle_file("clip.mp4", vp, True))
    assert first_part(s)["type"] == "video_url"
    assert s.captured["model"] == CFG["services"]["openai"]["video_model"]

    ap = _tmp(".wav")
    asyncio.run(a._handle_file("a.wav", ap, True))
    assert first_part(s)["type"] == "input_audio"
    assert s.captured["model"] == CFG["services"]["openai"]["audio_model"]

    dp = _tmp(".pdf")
    asyncio.run(a._handle_file("d.pdf", dp, True))
    assert first_part(s)["type"] == "file"
    assert s.captured["model"] == CFG["services"]["openai"]["document_model"]
    print("[OK] agent._handle_file 路由：视频→video_url / 音频→input_audio / 文档→file")


def test_audio_url_exts():
    from agent import _AUDIO_URL_EXTS, _VIDEO_URL_EXTS, _AUDIO_EXTS
    assert _AUDIO_URL_EXTS, "音频直链扩展名集合不应为空"
    assert not (_AUDIO_URL_EXTS & _VIDEO_URL_EXTS), "音频直链与视频直链集合不应相交"
    assert _AUDIO_URL_EXTS <= _AUDIO_EXTS, "音频直链集合应是音频扩展名的子集"
    print("[OK] _AUDIO_URL_EXTS 与 _VIDEO_URL_EXTS 不相交，且是 _AUDIO_EXTS 子集")


def test_media_url_routing():
    s = FakeServices(CFG)
    a = Agent(FakeLLM(), s)

    # 音频直链 → input_audio + audio_model
    out = asyncio.run(a._describe_media_url("https://x.com/a.mp3"))
    assert out and "音频链接内容" in out, out
    assert first_part(s)["type"] == "input_audio"
    assert s.captured["model"] == CFG["services"]["openai"]["audio_model"]

    # 视频直链 → video_url
    out = asyncio.run(a._describe_media_url("https://x.com/a.mp4"))
    assert "视频链接内容" in out, out
    assert first_part(s)["type"] == "video_url"

    # 文档直链 → file
    out = asyncio.run(a._describe_media_url("https://x.com/a.pdf"))
    assert "文档链接内容" in out, out
    assert first_part(s)["type"] == "file"

    # 图片直链（带 query 也要能识别）→ image_url
    out = asyncio.run(a._describe_media_url("https://x.com/a.png?v=1"))
    assert "图片链接内容" in out, out
    assert first_part(s)["type"] == "image_url"

    # 普通网页 → None（交回 fetch_url 抓文本）
    assert asyncio.run(a._describe_media_url("https://x.com/news/123")) is None
    print("[OK] _describe_media_url：音/视/图/文档四类分别走对应模型，普通网页返回 None")


def test_tool_fetch_url_media():
    s = FakeServices(CFG)
    s.web_config = {"page_media": False}  # 本机不联网，跳过页面媒体提取
    a = Agent(FakeLLM(), s)
    called = {"n": 0}

    async def fake_fetch(url, max_chars=3500, web_cfg=None):
        called["n"] += 1
        return "纯文本网页内容"

    # 打桩在实例上：fetch_url 现在是实例方法，按实例覆盖最稳
    s.fetch_url = fake_fetch
    try:
        # 媒体直链：不得走 fetch_url，必须交多模态模型
        out = asyncio.run(a._exec_tool("fetch_url", {"url": "https://x.com/a.mp3"}, {}))
        assert "媒体直链" in out, out
        assert called["n"] == 0, "媒体直链不应走 fetch_url"
        assert first_part(s)["type"] == "input_audio"

        # 普通网页：仍走 fetch_url
        out2 = asyncio.run(a._exec_tool("fetch_url", {"url": "https://x.com/news"}, {}))
        assert "网页内容" in out2 and called["n"] == 1, (out2, called)
    finally:
        del s.fetch_url
    print("[OK] _exec_tool(fetch_url)：媒体直链转多模态模型，普通网页仍抓文本")


def test_normalize_audio_url():
    s = FakeServices(CFG)
    a = Agent(FakeLLM(), s)
    ui = {"text": "听听 https://x.com/a.mp3 这个", "images": [], "voices": [], "files": []}
    text = asyncio.run(a._normalize(ui, None))
    assert "音频链接内容" in text, text
    assert first_part(s)["type"] == "input_audio"
    assert s.captured["model"] == CFG["services"]["openai"]["audio_model"]
    print("[OK] _normalize 文本内音频直链 → understand_audio(is_url=True)")


# ---------------------- 网页内嵌媒体（页面里的视频/音频/图片） ----------------------
class _FakeResp:
    """伪造 urlopen 的响应，供 fetch_page_media 的静态 HTML 阶段使用。"""

    def __init__(self, body: bytes, ctype="text/html; charset=utf-8"):
        self._body = body
        self.headers = {"Content-Type": ctype, "Content-Encoding": ""}

    def read(self, n=None):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_page_media_parser():
    from services import _PageMediaParser
    html = """<html><head>
      <title>示例页</title>
      <meta property="og:video" content="/media/clip.mp4">
      <meta property="og:image" content="//cdn.ex.com/cover.jpg">
      <meta property="og:audio" content="https://cdn.ex.com/pod.mp3">
      <link rel="alternate" type="application/pdf" href="/alt/paper.pdf">
    </head><body>
      <video src="/v/sub.mp4" poster="/v/p.jpg"></video>
      <audio src="/a/x.mp3"></audio>
      <video><source src="/v/s.webm" type="video/webm"></video>
      <a href="/files/report.pdf">下载报告</a>
      <a href="/news/other.html">其他新闻</a>
      <iframe src="/preview/manual.pdf"></iframe>
      <object data="/dl/slides.pptx"></object>
      <iframe src="https://player.bilibili.com/player.html?bvid=BV1xx"></iframe>
      <iframe src="https://ads.example.com/frame"></iframe>
    </body></html>"""
    p = _PageMediaParser()
    p.feed(html)
    assert p.title == "示例页", p.title
    assert "/media/clip.mp4" in p.videos and "/v/sub.mp4" in p.videos, p.videos
    assert "/v/s.webm" in p.videos, p.videos
    assert "//cdn.ex.com/cover.jpg" in p.images and "/v/p.jpg" in p.images, p.images
    assert "https://cdn.ex.com/pod.mp3" in p.audios and "/a/x.mp3" in p.audios, p.audios
    assert any("bilibili" in e for e in p.embeds), p.embeds
    assert not any("ads.example.com" in e for e in p.embeds), p.embeds
    # 文档：a 附件链接 / iframe 内嵌预览 / object / link alternate
    for d in ("/files/report.pdf", "/preview/manual.pdf",
              "/dl/slides.pptx", "/alt/paper.pdf"):
        assert d in p.docs, (d, p.docs)
    assert not any("other.html" in d for d in p.docs), p.docs
    print("[OK] _PageMediaParser：og 元信息/原生标签/平台 iframe/文档(附件·预览·object·alternate) 均能提取")


def test_is_doc_url():
    from services import _is_doc_url, _is_doc_mime, _guess_ext_from_ctype
    assert _is_doc_url("https://x.com/a.pdf")
    assert _is_doc_url("https://x.com/a.DOCX?v=1")      # 大小写 + query
    assert _is_doc_url("https://x.com/p.xlsx#page=2")
    assert not _is_doc_url("https://x.com/a.html")
    assert not _is_doc_url("https://x.com/article")
    assert _is_doc_mime("application/pdf")
    assert _is_doc_mime("application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert not _is_doc_mime("text/html")
    assert _guess_ext_from_ctype("application/pdf") == ".pdf"
    assert _guess_ext_from_ctype("application/pdf; charset=binary") == ".pdf"
    print("[OK] 文档 URL/MIME 判定与扩展名推断正确")


def test_fetch_page_media_absolute():
    import services as svc_mod
    html = b"""<html><head><title>T</title>
      <meta property="og:image" content="/img/a.jpg">
      <meta property="og:video" content="media/clip.mp4">
    </head></html>"""
    orig = svc_mod.urllib.request.urlopen
    svc_mod.urllib.request.urlopen = lambda *a, **k: _FakeResp(html)
    try:
        r = asyncio.run(Services(CFG).fetch_page_media(
            "https://ex.com/news/1.html", web_cfg={"headless_browser": False}))
    finally:
        svc_mod.urllib.request.urlopen = orig
    assert r["images"] == ["https://ex.com/img/a.jpg"], r
    assert r["videos"] == ["https://ex.com/news/media/clip.mp4"], r
    assert r["title"] == "T", r
    print("[OK] fetch_page_media：相对 URL 正确转绝对")


class PageMediaFake(FakeServices):
    """在 FakeServices 基础上替换页面媒体抓取与四类理解方法。"""

    def __init__(self, cfg, page):
        super().__init__(cfg)
        self.page = page
        self.calls = []
        self.doc_fail = False
        self.downloaded = None
        self.probe_result = ""

    async def fetch_page_media(self, url, web_cfg=None, max_each=3):
        return self.page

    async def understand_video(self, src, prompt=None, is_url=False, fps=None):
        self.calls.append(("video", src, is_url))
        return "视频描述"

    async def understand_audio(self, src, prompt=None, is_url=False, model=None):
        self.calls.append(("audio", src, is_url))
        return "音频描述"

    async def describe_image(self, src, prompt=None, is_url=False):
        self.calls.append(("image", src, is_url))
        return "图片描述"

    async def understand_document(self, src, prompt=None, is_url=False, model=None):
        self.calls.append(("doc", src, is_url))
        if self.doc_fail and is_url:
            raise ServiceUnavailable("forced doc url fail")
        return "文档描述"

    def download_file(self, url, max_mb=30):
        self.downloaded = url
        return _tmp(".pdf")

    def probe_content_type(self, url, timeout=6.0):
        return self.probe_result


def test_describe_page_media_routing():
    cfg = copy.deepcopy(CFG)
    cfg["web"] = {"page_media": True, "media_max_videos": 1, "media_max_images": 1}
    page = {"title": "T",
            "videos": ["https://ex.com/a.mp4"],
            "audios": ["https://ex.com/b.mp3"],
            "images": ["https://ex.com/c.jpg"],
            "embeds": []}
    s = PageMediaFake(cfg, page)
    a = Agent(FakeLLM(), s)
    out = asyncio.run(a._describe_page_media("https://ex.com/p"))
    assert out and "视频描述" in out and "音频描述" in out and "图片描述" in out, out
    assert [c[0] for c in s.calls] == ["video", "audio", "image"], s.calls
    assert all(c[2] is True for c in s.calls), s.calls  # 都按直链(is_url)传

    # 开关关闭 → 不分析
    cfg2 = copy.deepcopy(CFG)
    cfg2["web"] = {"page_media": False}
    s2 = PageMediaFake(cfg2, page)
    a2 = Agent(FakeLLM(), s2)
    assert asyncio.run(a2._describe_page_media("https://ex.com/p")) is None
    assert s2.calls == []
    print("[OK] _describe_page_media：视频/音频/图片分别路由；page_media=false 时不分析")


def test_describe_page_media_embed_download():
    cfg = copy.deepcopy(CFG)
    cfg["web"] = {"page_media": True, "media_max_videos": 1}
    page = {"title": "", "videos": [], "audios": [], "images": [],
            "embeds": ["https://www.bilibili.com/video/BV1xx"]}
    s = PageMediaFake(cfg, page)
    tmpf = _tmp(".mp4")
    s.download_media = lambda url, secs=180, mb=40: (tmpf, {"title": "测试视频"})
    a = Agent(FakeLLM(), s)
    out = asyncio.run(a._describe_page_media("https://www.bilibili.com/video/BV1xx"))
    assert out and "测试视频" in out and "视频描述" in out, out
    # 平台播放器 → 下载后按本地文件（is_url=False）交给视频模型
    assert s.calls and s.calls[0] == ("video", tmpf, False), s.calls
    print("[OK] _describe_page_media：平台播放器 → yt-dlp 下载 → 本地视频模型")


def test_describe_page_media_docs():
    cfg = copy.deepcopy(CFG)
    cfg["web"] = {"page_media": True, "media_max_docs": 1}
    page = {"title": "", "videos": [], "audios": [], "images": [], "embeds": [],
            "docs": ["https://ex.com/files/report.pdf"]}

    # 1) 直链可拉取 → 直接 file_url 交文档模型
    s = PageMediaFake(cfg, page)
    a = Agent(FakeLLM(), s)
    out = asyncio.run(a._describe_page_media("https://ex.com/p"))
    assert out and "页面内文档" in out and "文档描述" in out, out
    assert s.calls[0] == ("doc", "https://ex.com/files/report.pdf", True), s.calls

    # 2) 防盗链拉不到 → 本机下载后再走 Files API（is_url=False）
    s2 = PageMediaFake(cfg, page)
    s2.doc_fail = True
    a2 = Agent(FakeLLM(), s2)
    out2 = asyncio.run(a2._describe_page_media("https://ex.com/p"))
    assert out2 and "文档描述" in out2, out2
    assert s2.downloaded == "https://ex.com/files/report.pdf", s2.downloaded
    assert len(s2.calls) == 2 and s2.calls[1][0] == "doc" and s2.calls[1][2] is False, s2.calls
    print("[OK] _describe_page_media：页面文档直链→文档模型；失败→下载后上传（均已验证）")


def test_probe_content_type_routing():
    cfg = copy.deepcopy(CFG)
    cfg["web"] = {"page_media": True, "probe_content_type": True}
    s = PageMediaFake(cfg, {})

    s.probe_result = "application/pdf"
    a = Agent(FakeLLM(), s)
    out = asyncio.run(a._describe_media_url("https://ex.com/download?id=123"))
    assert out and "文档链接内容" in out, out
    assert s.calls[0][0] == "doc" and s.calls[0][2] is True, s.calls

    s.probe_result = "video/mp4"
    out = asyncio.run(a._describe_media_url("https://ex.com/get?id=9"))
    assert out and "视频链接内容" in out, out

    s.probe_result = "text/html; charset=utf-8"
    assert asyncio.run(a._describe_media_url("https://ex.com/article/9")) is None
    print("[OK] 无扩展名直链按 Content-Type 路由；普通网页仍交给 fetch_url")


def test_download_media_duration_guard():
    import yt_dlp

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            return {"duration": 9999, "title": "超长视频"}

    orig = yt_dlp.YoutubeDL
    yt_dlp.YoutubeDL = FakeYDL
    try:
        try:
            Services.download_media("https://ex.com/v", max_seconds=60)
            raise AssertionError("超时长应抛 ServiceUnavailable")
        except ServiceUnavailable as exc:
            assert "超过上限" in str(exc), exc
    finally:
        yt_dlp.YoutubeDL = orig
    print("[OK] download_media：超时长视频在下载前被拦下（不白下载）")


def test_video_page_detection():
    from services import _is_video_page
    assert _is_video_page("https://www.bilibili.com/video/BV1xx411c7mD")
    assert _is_video_page("https://b23.tv/abc")
    assert _is_video_page("https://www.youtube.com/watch?v=xxx")
    assert _is_video_page("https://youtu.be/xxx")
    assert _is_video_page("https://www.douyin.com/video/123")
    # 同域名但非视频页 → 不误判（否则会白白触发下载）
    assert not _is_video_page("https://www.bilibili.com/read/cv123456")
    assert not _is_video_page("https://news.qq.com/rain/a/20240101A00")
    assert not _is_video_page("https://example.com/page.html")
    print("[OK] _is_video_page：视频播放页命中，专栏/新闻页不误判")


def test_describe_platform_video():
    cfg = copy.deepcopy(CFG)
    cfg["web"] = {"page_media": True}
    page = {"title": "", "videos": [], "audios": [], "images": [], "embeds": []}

    s = PageMediaFake(cfg, page)
    tmpf = _tmp(".mp4")
    s.download_media = lambda url, secs=180, mb=40: (tmpf, {"title": "B站视频", "duration": 120})
    a = Agent(FakeLLM(), s)
    out = asyncio.run(a._describe_platform_video("https://www.bilibili.com/video/BV1xx"))
    assert out and "B站视频" in out and "视频描述" in out and "时长120秒" in out, out
    assert s.calls[0] == ("video", tmpf, False), s.calls

    # 下载失败 → None（调用方回退网页抓取，至少给标题简介）
    def boom(url, secs=180, mb=40):
        raise ServiceUnavailable("视频时长超过上限")

    s2 = PageMediaFake(cfg, page)
    s2.download_media = boom
    a2 = Agent(FakeLLM(), s2)
    assert asyncio.run(a2._describe_platform_video("https://www.bilibili.com/video/BV1xx")) is None
    print("[OK] _describe_platform_video：下载成功→本地视频模型；失败→None 回退网页")


def test_media_url_routes_video_page():
    cfg = copy.deepcopy(CFG)
    cfg["web"] = {"page_media": True}
    s = PageMediaFake(cfg, {})
    tmpf = _tmp(".mp4")
    s.download_media = lambda url, secs=180, mb=40: (tmpf, {"title": "B站标题", "duration": 60})
    a = Agent(FakeLLM(), s)
    out = asyncio.run(a._describe_media_url("https://www.bilibili.com/video/BV1xx"))
    assert out and "视频页面" in out and "B站标题" in out, out
    print("[OK] _describe_media_url：B站视频页 → 下载后看画面（不再只给元数据）")


def test_html_cache_reuse():
    import services as svc_mod
    calls = {"n": 0}
    html = (b"<html><head><meta property='og:image' content='/a.jpg'></head>"
            b"<body>hello</body></html>")
    orig = svc_mod.urllib.request.urlopen

    def fake(*a, **k):
        calls["n"] += 1
        return _FakeResp(html)

    svc_mod.urllib.request.urlopen = fake
    try:
        url = "https://cache-check.ex.com/p"
        _s = Services(CFG)   # fetch_url / fetch_page_media 已是实例方法
        asyncio.run(_s.fetch_url(url, web_cfg={"headless_browser": False}))
        n1 = calls["n"]
        asyncio.run(_s.fetch_page_media(url, web_cfg={"headless_browser": False}))
        n2 = calls["n"]
    finally:
        svc_mod.urllib.request.urlopen = orig
    assert n1 == 1, n1
    assert n2 == 1, f"fetch_page_media 应复用缓存，实际多请求了 {n2 - n1} 次"
    print("[OK] HTML 缓存：fetch_url 抓过的页面，fetch_page_media 直接复用不再下载")


def test_stream_upload_multipart():
    """流式上传：验证 multipart 结构、Content-Length、鉴权头、文件内容完整。"""
    import http.server
    import threading
    from pathlib import Path as _Path

    got = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            got["body"] = self.rfile.read(length)
            got["ctype"] = self.headers.get("Content-Type", "")
            got["auth"] = self.headers.get("Authorization", "")
            payload = b'{"id": "file-stream-1"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.handle_request, daemon=True)
    th.start()

    s = FakeServices(CFG)
    s.base_url = f"http://127.0.0.1:{port}"
    p = _tmp(".mp4")
    try:
        # 绕过 FakeServices 覆写的桩，直接测真实实现
        fid = Services._upload_file(s, _Path(p))
    finally:
        th.join(timeout=5)
        srv.server_close()

    assert fid == "file-stream-1", fid
    assert got["auth"] == "Bearer ark-test", got["auth"]
    assert "multipart/form-data; boundary=" in got["ctype"], got["ctype"]
    body = got["body"]
    assert b'name="file"; filename=' in body, body[:200]
    assert b'name="purpose"' in body
    assert body.startswith(b"--"), body[:20]
    assert body.rstrip().endswith(b"--"), body[-40:]
    assert b"\x00" * 32 in body, "文件内容未完整发送"
    print("[OK] 流式上传：multipart 结构/鉴权/文件内容完整，Content-Length 自动精确")


def test_audio_ogg_transcode():
    """ogg(opus) 必须先转 mp3；且优先走 Files API（file_id），失败才回退内联 Base64。"""
    import base64
    import shutil
    import subprocess
    from services import Services, ServiceUnavailable, _ffmpeg_exe

    ff = _ffmpeg_exe("ffmpeg")
    if not ff:
        print("[SKIP] 未找到 ffmpeg，跳过 ogg 转码测试")
        return

    work = Path(tempfile.mkdtemp(prefix="qqbot-ogg-"))
    src = work / "t.ogg"
    try:
        subprocess.run(
            [ff, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
             "-c:a", "libopus", str(src)],
            check=True, capture_output=True, timeout=60)
    except Exception as exc:  # noqa: BLE001
        print("[SKIP] 生成 opus/ogg 失败（ffmpeg 可能缺 libopus）:", exc)
        shutil.rmtree(work, ignore_errors=True)
        return

    s = Services(CFG)
    try:
        # ① 上传成功 → 直接用 file_id，不把音频塞进内存
        s._upload_file = lambda p, purpose="user_data", preprocess_configs=None: "file-fake-1"
        ref = asyncio.run(s._ingest_media(str(src), content_type="input_audio"))
        assert ref == {"file_id": "file-fake-1"}, ref

        # ② 上传失败 → 回退内联，且格式必须是转码后的 mp3
        def boom(p, purpose="user_data", preprocess_configs=None):
            raise ServiceUnavailable("forced upload failure")

        s._upload_file = boom
        ref2 = asyncio.run(s._ingest_media(str(src), content_type="input_audio"))
        assert ref2.get("format") == "mp3", f"回退时应是 mp3，实际 {ref2.get('format')}"
        raw = base64.b64decode(ref2["data"])
        assert raw[:3] == b"ID3" or raw[:1] == b"\xff", ("不是合法 mp3 头", raw[:4])
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("[OK] 音频：ogg/opus 转 mp3 → 优先 Files API(file_id)，失败回退内联 Base64")


def test_video_with_audio_modality():
    """本地视频必须把音轨作为独立 input_audio 一起送，否则模型听不到声音。"""
    p = _tmp(".mp4")

    async def fake_extract(video):
        return {"file_id": "aud-fid-9"}, None

    # 开关打开（默认）：应有 video_url + input_audio 两个媒体段
    s = FakeServices(CFG)
    s._extract_audio_for_video = fake_extract
    asyncio.run(s.understand_video(p))
    content = s.captured["messages"][0]["content"]
    types = [c.get("type") for c in content]
    assert "video_url" in types and "input_audio" in types, types
    audio = next(c for c in content if c.get("type") == "input_audio")
    assert audio["input_audio"]["file_id"] == "aud-fid-9", audio
    assert types[-1] == "text", types

    # 开关关闭：退回纯画面分析
    s2 = FakeServices(CFG)
    s2.video_with_audio = False
    s2._extract_audio_for_video = fake_extract
    asyncio.run(s2.understand_video(_tmp(".mp4")))
    types2 = [c.get("type") for c in s2.captured["messages"][0]["content"]]
    assert "input_audio" not in types2, types2

    print("[OK] 视频：音轨以独立 input_audio 送出（画面+声音联合理解），开关可关闭")


def test_retention_policy():
    """默认保留远端文件（7 天自动过期），只有 retention=immediate 才删。"""
    from services import Services

    s = Services(CFG)
    assert s.files_retention == "keep", s.files_retention
    calls = []
    s._delete_file = lambda fid: calls.append(fid)

    # keep 模式：分析结束不删，供多轮追问复用 file_id
    asyncio.run(s._cleanup_remote({"file_id": "f1"}))
    assert calls == [], "keep 模式不应删除远端文件"

    # immediate 模式：立即删除
    s.files_retention = "immediate"
    asyncio.run(s._cleanup_remote({"file_id": "f2"}))
    assert calls == ["f2"], calls

    # 内联回退（无 file_id）不触发删除
    asyncio.run(s._cleanup_remote({}))
    assert calls == ["f2"], calls

    print("[OK] 远端保留策略：keep 不删（7 天自动过期）/ immediate 立即删")


# ---------------------- 媒体编号 / 追问（inspect_media） ----------------------
def test_media_register_and_trim():
    s = FakeServices(CFG)
    a = Agent(FakeLLM(), s)
    entry = {}
    h1 = a._register_media(entry, "image", {"file_id": "f1"}, msg_id="100", seg=0)
    h2 = a._register_media(entry, "video", {"file_id": "f2"}, msg_id="101", seg=2,
                           audio_ref={"file_id": "a2"})
    assert (h1, h2) == ("m1", "m2"), (h1, h2)
    assert entry["media_seq"] == 2
    rec = entry["media"]["m1"]
    assert rec["file_id"] == "f1" and rec["msg_id"] == "100" and rec["seg"] == 0
    assert entry["media"]["m2"]["audio_file_id"] == "a2", "视频必须连音轨 id 一起登记"

    # 内联 base64 不登记（几十 MB 字符串不该进记忆）
    assert a._register_media(entry, "image", {"url": "data:image/png;base64,AAAA"}) is None

    # 编号单调递增、永不复用；超过上限淘汰最旧的，但新号继续变大
    for i in range(80):
        a._register_media(entry, "image", {"file_id": f"g{i}"}, msg_id=str(200 + i), seg=0)
    assert len(entry["media"]) <= 60, len(entry["media"])
    assert "m1" not in entry["media"], "最旧的应被淘汰"
    assert entry["media_seq"] == 82, entry["media_seq"]
    assert a._next_handle(entry) == "m83"
    print("[OK] 媒体登记：自增编号、视频带音轨 id、base64 不登记、超上限淘汰最旧")


def test_normalize_media_handle():
    s = FakeServices(CFG)
    a = Agent(FakeLLM(), s)
    entry = {}

    ui = {"text": "这是什么", "images": [_tmp(".png")], "voices": [], "videos": [],
          "files": [], "msg_id": "900", "segs": {"images": [0]}}
    text = asyncio.run(a._normalize(ui, None, entry))
    assert "[图片 m1 · 内容]" in text, text
    rec = entry["media"]["m1"]
    assert rec["kind"] == "image" and rec["file_id"] == "file-fake-123", rec
    assert rec["msg_id"] == "900" and rec["seg"] == 0, rec

    # 引用/转发来的媒体（segs=-1）没有可靠的消息定位，只登记 file_id
    ui2 = {"text": "", "images": [_tmp(".png")], "voices": [], "videos": [],
           "files": [], "msg_id": "901", "segs": {"images": [-1]}}
    asyncio.run(a._normalize(ui2, None, entry))
    rec2 = entry["media"]["m2"]
    assert "msg_id" not in rec2, rec2

    # 单测/无 entry 场景：不登记，保持旧的 [图片内容] 文案
    text3 = asyncio.run(a._normalize({"text": "", "images": [_tmp(".png")],
                                      "voices": [], "videos": [], "files": []}, None))
    assert "[图片内容]" in text3, text3
    print("[OK] _normalize：媒体登记为 m1/m2 并写进文本；引用来的不留 msg_id；无 entry 不登记")


def test_inspect_media_reuse():
    s = FakeServices(CFG)
    a = Agent(FakeLLM(), s)
    entry = {"media_seq": 1, "media": {"m1": {
        "kind": "image", "file_id": "file-x", "msg_id": "900", "seg": 0,
        "expire_at": time.time() + 86400 * 20}}}
    out = asyncio.run(a._inspect_media(entry, "m1", "右下角的字是什么", None))
    assert out == "OK", out
    part = first_part(s)
    assert part["type"] == "image_url" and part["image_url"]["file_id"] == "file-x", part
    assert "右下角的字是什么" in s.captured["messages"][0]["content"][-1]["text"]
    assert s.last_preprocess is None, "追问不应触发上传"

    # 编号容错：模型可能写成「图片 m1」或「[m1]」
    s.captured = None
    out2 = asyncio.run(a._inspect_media(entry, "图片 m1", "再看下角的字", None))
    assert out2 == "OK" and first_part(s)["image_url"]["file_id"] == "file-x"

    # 未知编号：明确告知可用编号，且不发任何模型请求
    s.captured = None
    out3 = asyncio.run(a._inspect_media(entry, "m9", "看一下", None))
    assert "没有 m9" in out3 and s.captured is None, out3
    print("[OK] inspect_media：直接复用 file_id 不重传；编号容错；未知编号有明确提示")


def test_inspect_media_expired():
    s = FakeServices(CFG)
    s.file_exists_ok = False          # 远端已 404
    a = Agent(FakeLLM(), s)
    entry = {"media_seq": 1, "media": {"m1": {
        "kind": "image", "file_id": "file-dead",
        "expire_at": time.time() - 60}}}       # 已过期，且没有 msg_id 可重下
    out = asyncio.run(a._inspect_media(entry, "m1", "这是什么", None))
    assert "已不可用" in out, out
    assert "重发" in out
    assert s.captured is None, "不可用时不应发出模型请求"

    # 只剩直链：仍可追问（不重传）
    entry["media"]["m1"] = {"kind": "image", "url": "https://x.com/a.png",
                            "expire_at": time.time() - 60}
    out2 = asyncio.run(a._inspect_media(entry, "m1", "这是什么", None))
    assert out2 == "OK" and first_part(s)["image_url"]["url"] == "https://x.com/a.png"

    # 缺少具体问题时要求补充，而不是拿空问题去调模型
    s.captured = None
    out3 = asyncio.run(a._inspect_media(entry, "m1", "  ", None))
    assert "具体问题" in out3 and s.captured is None, out3
    print("[OK] inspect_media：过期+无重下线索 → 明确告知；有直链仍可用；空问题被挡回")


def test_handle_timestamp():
    class CapLLM(FakeLLM):
        def __init__(self):
            self.seen = None

        async def complete(self, messages, tools=None):
            self.seen = copy.deepcopy(messages)
            return {"choices": [{"message": {"content": "嗨"}}]}

    s = FakeServices(CFG)
    llm = CapLLM()
    a = Agent(llm, s)
    ctx = {}
    asyncio.run(a.handle("p1", {"text": "你好", "images": [], "voices": [], "videos": [],
                                "files": []}, ctx, {"mode": "accumulate"}, None))
    last = llm.seen[-1]
    assert last["role"] == "user", last
    assert re.match(r"^\[\d\d-\d\d \d\d:\d\d\]\n你好$", last["content"]), last["content"]
    # 落进记忆的也是带时间戳的版本，历史里能看出每句是什么时候说的
    assert re.match(r"^\[\d\d-\d\d \d\d:\d\d\]\n你好$", ctx["p1"]["recent"][0]["content"])
    print("[OK] 时间戳：只加在本轮 user 消息（前缀稳定），历史里同样保留")


def test_tool_loop_inspect_media():
    """端到端串起来：首轮登记 m1 → 模型发起 inspect_media → 复用 file_id，不重传。"""

    class ToolLLM(FakeLLM):
        def __init__(self):
            self.calls = 0
            self.seen = []

        async def complete(self, messages, tools=None):
            self.seen.append(copy.deepcopy(messages))
            self.calls += 1
            if self.calls == 1:
                return {"choices": [{"message": {"content": "", "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "inspect_media",
                                 "arguments": json.dumps({"handle": "m1",
                                                          "question": "画面主色调是红色吗"})}}]}}]}
            return {"choices": [{"message": {"content": "是的，是红色。"}}]}

    s = FakeServices(CFG)
    llm = ToolLLM()
    a = Agent(llm, s)
    ctx = {}

    async def fake_call_api(action, params):
        return {"status": "ok", "data": {"message": []}}

    segs = asyncio.run(a.handle("p1", {"text": "看看这张图", "images": [_tmp(".png")],
                                       "voices": [], "videos": [], "files": [],
                                       "msg_id": "900", "segs": {"images": [0]}},
                                ctx, {"mode": "accumulate"}, fake_call_api))

    entry = ctx["p1"]
    assert "m1" in entry["media"], entry.get("media")
    assert "[图片 m1 · 内容]" in entry["recent"][0]["content"], entry["recent"][0]
    assert s.uploads == 1, f"首轮只应上传一次，实际 {s.uploads}"

    # 第二轮上下文里应出现 tool 结果（模型据此给出结论）
    tool_msgs = [m for m in llm.seen[-1] if m.get("role") == "tool"]
    assert tool_msgs and "OK" in tool_msgs[-1]["content"], tool_msgs
    assert s.uploads == 1, "追问必须复用 file_id，不得重新上传"
    assert segs[0]["data"]["text"] == "是的，是红色。"
    print("[OK] 全链路：handle→登记 m1→inspect_media→复用 file_id（全程只上传 1 次）")


def test_first_prompt_extraction():
    s = FakeServices(CFG)
    a = Agent(FakeLLM(), s)
    cq = a._clean_question

    assert cq("这是什么") == "这是什么"
    assert cq("御坂：看看这张图") == "看看这张图", "群聊发送者前缀要剥掉"
    assert cq("[引用的消息 · 张三：他说了啥]\n右下角是什么字") == "右下角是什么字"
    assert cq("[分享链接] https://x.com/a\n这是什么字") == "这是什么字"
    assert cq("https://x.com/a.png") == "", "纯链接不算问题"
    assert cq("哈哈哈哈") == "", "短且不像提问 → 不硬塞给视觉模型"
    assert cq("") == ""
    assert cq("明天：怎么样") == "怎么样"

    # 群聊里"发送者前缀 + 引用 + 原始问题"叠在一起时，只保留原始问题
    combined = ("御坂：[引用的消息 · 张三：这张图拍的是哪里]\n"
                "右下角那家店叫什么")
    assert cq(combined) == "右下角那家店叫什么", cq(combined)

    # 合并转发的聊天记录不是"用户的问题"，必须整段丢弃
    fwd = ("这些人说了什么？\n"
           "[合并转发的聊天记录，共 3 条]\n"
           "1. 张三：明天开会\n2. 李四：[1张图片]\n3. 王五：好的")
    assert cq(fwd) == "这些人说了什么？", cq(fwd)
    assert cq("[合并转发的聊天记录，共 2 条]\n1. 张三：在吗") == "", "只转发不提问 → 不该产生问题"

    assert a._first_prompt("") is None, "无问题时应退回各类型的默认 prompt"
    p = a._first_prompt("他在喝什么")
    assert p and "他在喝什么" in p and "看不到" in p, p
    print("[OK] 首轮 prompt：剥前缀/引用/链接，短闲聊不塞，无问题用默认")


def test_normalize_passes_user_question():
    s = FakeServices(CFG)
    a = Agent(FakeLLM(), s)

    ui = {"text": "他在喝什么", "images": [_tmp(".png")], "voices": [], "videos": [],
          "files": [], "msg_id": "900", "segs": {"images": [0]}}
    asyncio.run(a._normalize(ui, None, {}))
    prompt = s.captured["messages"][0]["content"][-1]["text"]
    assert "他在喝什么" in prompt, prompt

    # 群聊前缀不会污染视觉模型的问题
    s2 = FakeServices(CFG)
    a2 = Agent(FakeLLM(), s2)
    ui2 = dict(ui, text="御坂：他在喝什么")
    asyncio.run(a2._normalize(ui2, None, {}))
    prompt2 = s2.captured["messages"][0]["content"][-1]["text"]
    assert "他在喝什么" in prompt2 and "御坂" not in prompt2, prompt2

    # 没有文字时保持原来的默认描述提示（行为不变）
    s3 = FakeServices(CFG)
    a3 = Agent(FakeLLM(), s3)
    ui3 = dict(ui, text="")
    asyncio.run(a3._normalize(ui3, None, {}))
    prompt3 = s3.captured["messages"][0]["content"][-1]["text"]
    assert "用户的问题是" not in prompt3 and "请客观描述" in prompt3, prompt3

    # 文件路由同样带上问题
    s4 = FakeServices(CFG)
    a4 = Agent(FakeLLM(), s4)
    asyncio.run(a4._handle_file("a.pdf", _tmp(".pdf"), True, prompt="第 3 页说了什么"))
    prompt4 = s4.captured["messages"][0]["content"][-1]["text"]
    assert "第 3 页说了什么" in prompt4, prompt4
    print("[OK] 首轮 prompt 已传入图片/文件；群聊前缀被剥离；无文字时行为不变")


# ---------------------- 联网搜索：DeepSeek 优先 + 引擎链保底 ----------------------
def _mk_search_svc(enable=True):
    s = FakeServices(CFG)
    s.ds_search_enable = enable
    s.ds_api_key = "ds-test"
    s.ds_base_url = "https://api.deepseek.com"
    return s


def test_deepseek_search_primary():
    s = _mk_search_svc()
    seen = {}

    def fake_raw(url, payload, headers, timeout=60):
        seen.update(url=url, payload=payload, headers=headers)
        return {"results": [
            {"title": "标题一", "url": "https://a.com/1", "summary": "摘要一"},
            {"title": "标题二", "url": "https://a.com/2", "snippet": "摘要二"},
        ]}

    s._post_json_raw = fake_raw
    out = asyncio.run(s.web_search("今天天气"))
    assert seen["url"].endswith("/v1/tools/search"), seen.get("url")
    assert seen["headers"].get("Authorization") == "Bearer ds-test"
    assert seen["payload"]["query"] == "今天天气"
    assert "标题一" in out and "https://a.com/1" in out, out
    assert "标题二" in out and "摘要二" in out, "无 summary 时应回退用 snippet"
    print("[OK] DeepSeek 搜索：走 /v1/tools/search，结果格式化正确")


def test_search_fallback_to_engines():
    """DeepSeek 不可用时必须落到原有引擎链（这里让网络全断，验证确实降级了）。"""
    s = _mk_search_svc()

    def boom(*a, **k):
        raise OSError("no network")

    s._post_json_raw = boom
    orig = services_mod._urlopen
    services_mod._urlopen = boom
    try:
        out = asyncio.run(s.web_search("今天天气"))
    finally:
        services_mod._urlopen = orig

    assert "联网搜索失败" in out, out
    assert "DeepSeek(官方)" in out, "失败提示里应列出已尝试过的 DeepSeek"
    print("[OK] DeepSeek 失败 → 回退引擎链；引擎也失败时给出友好提示")


def test_search_disabled_skips_deepseek():
    s = _mk_search_svc(enable=False)
    called = []

    def fake_raw(url, payload, headers, timeout=60):
        called.append(url)
        return {"results": [{"title": "x", "url": "https://x"}]}

    s._post_json_raw = fake_raw
    orig = services_mod._urlopen
    services_mod._urlopen = lambda *a, **k: (_ for _ in ()).throw(OSError("no net"))
    try:
        out = asyncio.run(s.web_search("今天天气"))
    finally:
        services_mod._urlopen = orig

    assert not called, "禁用时不应调用 DeepSeek 搜索"
    assert "DeepSeek" not in out, out
    print("[OK] deepseek_search.enable=false → 完全走原有引擎链")


def test_web_search_does_not_block_loop():
    """搜索期间事件循环必须还能转——否则 _loop 收不到 WS 数据，API 响应全超时。"""
    s = _mk_search_svc(enable=False)
    ticks = {"n": 0}
    done = {"stop": False}

    def slow_urlopen(req, timeout=None, **kw):
        time.sleep(0.3)                 # 模拟每个引擎都慢
        raise OSError("slow fail")

    orig = services_mod._urlopen
    services_mod._urlopen = slow_urlopen

    async def ticker():
        while not done["stop"]:
            await asyncio.sleep(0.01)
            ticks["n"] += 1

    async def main():
        t = asyncio.create_task(ticker())
        await s.web_search("测试")
        done["stop"] = True
        await t

    try:
        asyncio.run(main())
    finally:
        services_mod._urlopen = orig

    # 4 个引擎 × 0.3s ≈ 1.2s；若被阻塞，ticker 几乎转不动（<5）
    assert ticks["n"] > 5, f"事件循环被阻塞，ticker 只跑了 {ticks['n']} 次"
    print(f"[OK] web_search 不阻塞事件循环（期间 ticker 跑了 {ticks['n']} 次）")


def test_fetch_url_does_not_block_loop():
    s = Services(CFG)
    ticks = {"n": 0}
    done = {"stop": False}

    def slow_sync(url, max_chars, web_cfg):
        time.sleep(0.5)
        return "抓取到的内容", False

    s._fetch_url_sync = slow_sync      # 只让同步阶段变慢，浏览器阶段不参与

    async def ticker():
        while not done["stop"]:
            await asyncio.sleep(0.01)
            ticks["n"] += 1

    async def main():
        t = asyncio.create_task(ticker())
        out = await s.fetch_url("https://x.com/p",
                                web_cfg={"headless_browser": False})
        done["stop"] = True
        await t
        return out

    out = asyncio.run(main())
    assert out == "抓取到的内容", out
    assert ticks["n"] > 5, f"事件循环被阻塞，ticker 只跑了 {ticks['n']} 次"
    print(f"[OK] fetch_url 不阻塞事件循环（期间 ticker 跑了 {ticks['n']} 次）")


if __name__ == "__main__":
    test_video_fileid()
    test_video_base64_fallback()
    test_video_url()
    test_audio_local_inputaudio()
    test_audio_url_inputaudio()
    test_document_fileid()
    test_document_filedata_fallback()
    test_document_fileurl()
    test_image_fileid()
    test_agent_routing()
    test_audio_url_exts()
    test_media_url_routing()
    test_tool_fetch_url_media()
    test_normalize_audio_url()
    test_page_media_parser()
    test_is_doc_url()
    test_fetch_page_media_absolute()
    test_describe_page_media_routing()
    test_describe_page_media_embed_download()
    test_describe_page_media_docs()
    test_probe_content_type_routing()
    test_download_media_duration_guard()
    test_video_page_detection()
    test_describe_platform_video()
    test_media_url_routes_video_page()
    test_html_cache_reuse()
    test_stream_upload_multipart()
    test_audio_ogg_transcode()
    test_video_with_audio_modality()
    test_retention_policy()
    test_media_register_and_trim()
    test_normalize_media_handle()
    test_inspect_media_reuse()
    test_inspect_media_expired()
    test_handle_timestamp()
    test_tool_loop_inspect_media()
    test_first_prompt_extraction()
    test_normalize_passes_user_question()
    test_deepseek_search_primary()
    test_search_fallback_to_engines()
    test_search_disabled_skips_deepseek()
    test_web_search_does_not_block_loop()
    test_fetch_url_does_not_block_loop()
    print("\nALL MULTIMODAL TESTS PASSED")
