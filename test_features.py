"""离线验证本轮新增能力：文件文本抽取 / 链接抓取降级 / 扩展名路由。"""
import asyncio
import io
import zipfile
import tempfile
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import agent as ag
import services as svc


def test_read_text_like():
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write("# 标题\n这是一行测试文本 content=123\n")
        p = f.name
    out = ag._read_file_text(p)
    assert out and "测试文本" in out, out
    os.unlink(p)
    print("[OK] 文本类文件读取:", out[:30])


def test_read_office_docx():
    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           f'<w:document xmlns:w="{W}"><w:body>'
           f'<w:p><w:r><w:t>你好世界 docx 文本</w:t></w:r></w:p>'
           f'<w:p><w:r><w:t>第二段内容</w:t></w:r></w:p>'
           f'</w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    with tempfile.NamedTemporaryFile("wb", suffix=".docx", delete=False) as f:
        f.write(buf.getvalue()); p = f.name
    out = ag._read_file_text(p)
    assert out and "docx 文本" in out and "第二段内容" in out, out
    os.unlink(p)
    print("[OK] docx 抽取:", out.replace("\n", " / ")[:50])


def test_read_office_xlsx():
    # 用已安装的 openpyxl 生成一个合法 xlsx，真正走高精度读取路径
    if not ag._HAVE_OPENPYXL:
        print("[SKIP] openpyxl 不可用，跳过 xlsx 高精度测试")
        return
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["苹果", "香蕉"])
    ws.append(["橙子", 42])
    with tempfile.NamedTemporaryFile("wb", suffix=".xlsx", delete=False) as f:
        p = f.name
    wb.save(p)
    out = ag._read_file_text(p)
    assert out and "苹果" in out and "香蕉" in out, out
    os.unlink(p)
    print("[OK] xlsx 抽取(openpyxl):", out[:50])


def test_read_office_pptx():
    P = "http://schemas.openxmlformats.org/presentationml/2006/main"
    A = "http://schemas.openxmlformats.org/drawingml/2006/main"
    xml = (f'<?xml version="1.0"?><p:sld xmlns:p="{P}" xmlns:a="{A}">'
           f'<p:sp><p:txBody><a:p><a:r><a:t>幻灯片标题文本</a:t></a:r></a:p>'
           f'</p:txBody></p:sp></p:sld>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("ppt/slides/slide1.xml", xml)
    with tempfile.NamedTemporaryFile("wb", suffix=".pptx", delete=False) as f:
        f.write(buf.getvalue()); p = f.name
    out = ag._read_file_text(p)
    assert out and "幻灯片标题文本" in out, out
    os.unlink(p)
    print("[OK] pptx 抽取:", out[:50])


def test_read_pdf_best_effort():
    # 极简 PDF：内容流里放字面量 (Hello PDF Text)（PDF 字面量仅测抽取逻辑，故用 ASCII）
    pdf = b"%PDF-1.4\n1 0 obj<</Length 40>>stream\nBT /F1 12 Tf (Hello PDF Text) Tj ET\nendstream\nendobj\n"
    with tempfile.NamedTemporaryFile("wb", suffix=".pdf", delete=False) as f:
        f.write(pdf); p = f.name
    # 1) 直接验证简易兜底路径（不依赖 pypdf 能否解析该残缺 PDF）
    out_simple = ag._read_pdf_simple(p, 6000)
    assert out_simple and "Hello PDF Text" in out_simple, out_simple
    # 2) 集成入口应返回非空字符串（pypdf 成功则用其输出，否则回退简易提取）
    out = ag._read_file_text(p)
    assert out and isinstance(out, str) and len(out) > 0, out
    os.unlink(p)
    print("[OK] PDF 抽取(集成):", out[:50].replace("\n", " "))


def test_extension_routing():
    assert ".mp3" in ag._AUDIO_EXTS and ".silk" in ag._AUDIO_EXTS
    assert ".png" in ag._IMAGE_EXTS and ".heic" in ag._IMAGE_EXTS
    assert ".docx" in ag._OFFICE_EXTS
    print("[OK] 扩展名路由集合正确 (audio=%d image=%d office=%d)"
          % (len(ag._AUDIO_EXTS), len(ag._IMAGE_EXTS), len(ag._OFFICE_EXTS)))


def test_strip_html():
    html = "<html><head><style>x</style></head><body><script>bad()</script><p>可见文本 <b>加粗</b></p></body></html>"
    out = svc._strip_html(html)
    assert "可见文本" in out and "bad()" not in out and "<" not in out, repr(out)
    print("[OK] HTML 剥离:", out.strip()[:30])


async def test_fetch_url_graceful():
    # 沙箱无网络，应返回友好降级字符串而非抛异常
    # fetch_url 是实例方法（内部用 to_thread 跑同步抓取），需先实例化
    out = await svc.Services({}).fetch_url("https://nonexistent.invalid.example/xyz")
    assert isinstance(out, str) and len(out) > 0, out
    print("[OK] 坏链接抓取降级:", out[:50])


def test_search_parsers():
    baidu_html = ('''
<div class="c-container"><h3 class="t"><a href="x">北京天气预报</a></h3>
<div class="c-abstract">今天北京晴，气温 15~25 度。</div></div>
<div class="c-container"><h3 class="t"><a href="y">上海天气</a></h3>
<div class="c-abstract">上海多云转阴。</div></div>
''')
    sogou_html = ('''
<div class="rb"><h3><a href="a">Python 教程</a></h3>
<div class="text-layout">Python 广泛用于数据科学。</div></div>
<div class="rb"><h3><a href="b">深度学习入门</a></h3>
<div class="text-layout">从零开始学神经网络。</div></div>
''')
    b = svc._parse_baidu_results(baidu_html, 3)
    s = svc._parse_sogou_results(sogou_html, 3)
    assert any("北京天气预报" in r and "15~25" in r for r in b), b
    assert any("Python 教程" in r and "数据科学" in r for r in s), s
    # 反爬验证页（无结果结构）必须安全返回空，不残留 HTML 片段
    assert svc._parse_baidu_results("<html>验证页面</html>", 5) == []
    assert svc._parse_sogou_results("<html>verify</html>", 5) == []
    print("[OK] 百度/搜狗解析器 + 反爬降级:", b[0].split(chr(10))[0], "|", s[0].split(chr(10))[0])


def test_reply_reference():
    """引用回复：能从消息段取到被引用消息 ID，并还原其原文作为上下文。"""
    import main

    # --- 1. 提取 reply_id（数组型 / CQ 字符串型 / 无引用）---
    arr = [
        {"type": "reply", "data": {"id": "7483920156"}},
        {"type": "at", "data": {"qq": "2024514917"}},
        {"type": "text", "data": {"text": " 他说的是什么意思？"}},
    ]
    assert main.OneBotClient._extract_reply_id(arr) == "7483920156"
    assert main.OneBotClient._extract_reply_id(
        "[CQ:reply,id=-2147][CQ:at,qq=2024514917] 这啥意思") == "-2147"
    assert main.OneBotClient._extract_reply_id(
        [{"type": "text", "data": {"text": "普通消息"}}]) is None
    assert main.OneBotClient._extract_reply_id("") is None

    # --- 2. 没有 self 依赖的构造方式：绕过 __init__ 直接注入依赖 ---
    client = object.__new__(main.OneBotClient)
    client.bot_qq = "2024514917"

    async def make_client(payload, raises=False):
        async def fake_api(action, params, timeout=10):
            assert action == "get_msg", action
            assert params["message_id"] == "7483920156", params
            if raises:
                raise RuntimeError("模拟 get_msg 失败")
            return payload
        client._call_api = fake_api
        return client

    # 文本型被引用消息（群名片优先于昵称）
    text_payload = {"status": "ok", "data": {
        "user_id": 111222,
        "sender": {"nickname": "张三", "card": "张三(群名片)"},
        "message": [{"type": "text", "data": {"text": "明天几点开会？"}}],
    }}
    asyncio.run(make_client(text_payload))
    got, imgs, voices, vids, files = asyncio.run(client._fetch_quoted("7483920156"))
    assert "张三(群名片)" in got and "明天几点开会？" in got, got
    assert (imgs, voices, vids) == ([], [], []), "纯文本消息不应带任何媒体段"

    # 图片型：摘要提示数量，同时把图片段原样交出（供调用方下载后送视觉模型）
    img_payload = {"status": "ok", "data": {
        "user_id": 111222, "sender": {"nickname": "李四"},
        "message": [{"type": "image", "data": {"file": "a.jpg"}},
                    {"type": "image", "data": {"file": "b.jpg"}}],
    }}
    asyncio.run(make_client(img_payload))
    got, imgs, voices, vids, files = asyncio.run(client._fetch_quoted("7483920156"))
    assert "李四" in got and "2张图片" in got, got
    assert len(imgs) == 2 and imgs[0].get("file") == "a.jpg", imgs
    assert (voices, vids) == ([], []), (voices, vids)

    # 视频型：视频段必须一并返回，否则"引用视频问这是什么"无法分析
    vid_payload = {"status": "ok", "data": {
        "user_id": 111222, "sender": {"nickname": "王五"},
        "message": [{"type": "video", "data": {"file": "v.mp4", "url": "https://cdn/v.mp4"}}],
    }}
    asyncio.run(make_client(vid_payload))
    got, imgs, voices, vids, files = asyncio.run(client._fetch_quoted("7483920156"))
    assert "王五" in got and "1个视频" in got, got
    assert len(vids) == 1 and vids[0].get("file") == "v.mp4", vids
    assert (imgs, voices) == ([], []), (imgs, voices)

    # 语音型
    voice_payload = {"status": "ok", "data": {
        "user_id": 111222, "sender": {"nickname": "赵六"},
        "message": [{"type": "record", "data": {"file": "r.amr"}}],
    }}
    asyncio.run(make_client(voice_payload))
    got, imgs, voices, vids, files = asyncio.run(client._fetch_quoted("7483920156"))
    assert "1条语音" in got and len(voices) == 1, (got, voices)

    # 文件型：引用一个 PDF，文件段必须被带出（供调用方下载后交文档模型）
    file_payload = {"status": "ok", "data": {
        "user_id": 111222, "sender": {"nickname": "钱七"},
        "message": [{"type": "file", "data": {"file": "报告.pdf", "file_id": "fid-1",
                                              "url": "https://cdn/report.pdf"}}],
    }}
    asyncio.run(make_client(file_payload))
    got, imgs, voices, vids, files = asyncio.run(client._fetch_quoted("7483920156"))
    assert "1个文件" in got and "报告.pdf" in got, got
    assert len(files) == 1 and files[0]["name"] == "报告.pdf", files
    assert files[0]["file_id"] == "fid-1", files[0]
    assert (imgs, voices, vids) == ([], [], []), (imgs, voices, vids)

    # get_msg 抛异常 → 安全返回空，不影响主流程
    asyncio.run(make_client({}, raises=True))
    assert asyncio.run(client._fetch_quoted("7483920156")) == ("", [], [], [], [])

    # 返回空 data → 空结果
    asyncio.run(make_client({"status": "ok", "data": {}}))
    assert asyncio.run(client._fetch_quoted("7483920156")) == ("", [], [], [], [])

    print("[OK] 引用回复：reply_id 提取 + get_msg 还原原文（图/语音/视频段都带回）+ 异常降级")


def test_env_override_ws_url():
    """环境变量 NAPCAT_WS_URL 可覆盖配置文件里的 ws_url（本地/服务器共用一份配置）。"""
    import config as cfgmod
    import json as _json

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        _json.dump({"onebot": {"ws_url": "ws://napcat:3001", "token": "t0k"}}, f)
        p = f.name
    try:
        # 不设环境变量 → 用文件里的值
        os.environ.pop("NAPCAT_WS_URL", None)
        c1 = cfgmod.load_config(Path(p))
        assert c1["onebot"]["ws_url"] == "ws://napcat:3001", c1["onebot"]

        # 设了环境变量 → 覆盖
        os.environ["NAPCAT_WS_URL"] = "ws://127.0.0.1:3001"
        try:
            c2 = cfgmod.load_config(Path(p))
        finally:
            os.environ.pop("NAPCAT_WS_URL", None)
        assert c2["onebot"]["ws_url"] == "ws://127.0.0.1:3001", c2["onebot"]
        assert c2["onebot"]["token"] == "t0k", "其他字段不应被影响"
    finally:
        os.unlink(p)
    print("[OK] 环境变量覆盖 ws_url：本地与服务器可共用同一份 config.json")


def test_memory_save_busy_fallback():
    """Docker 单文件挂载时 rename 会 EBUSY，应回退为覆盖写入而不是丢掉记忆。"""
    import main
    import json as _json

    d = tempfile.mkdtemp()
    p = Path(d) / "memory.json"
    p.write_text('{"old": 1}', encoding="utf-8")

    client = object.__new__(main.OneBotClient)
    client._mem_enabled = True
    client._mem_path = p
    client._mem_dirty = True
    client.context = {"p123": {"recent": [{"role": "user", "content": "你好"}]}}

    # --- 1. 正常路径：原子替换成功 ---
    client._save_memory()
    assert _json.loads(p.read_text(encoding="utf-8")) == client.context
    assert not (Path(d) / "memory.json.tmp").exists()

    # --- 2. 模拟 bind mount：os.replace 抛 EBUSY → 回退覆盖写入 ---
    client._mem_dirty = True
    client.context = {"p456": {"recent": [{"role": "user", "content": "第二次"}]}}
    real_replace = main.os.replace

    def fake_replace(src, dst):
        raise OSError(16, "Device or resource busy")

    main.os.replace = fake_replace
    try:
        client._save_memory()
    finally:
        main.os.replace = real_replace

    got = _json.loads(p.read_text(encoding="utf-8"))
    assert got == client.context, got
    assert not (Path(d) / "memory.json.tmp").exists(), "临时文件应被清理"
    assert client._mem_dirty is False, "保存成功后 dirty 应复位"

    import shutil as _sh
    _sh.rmtree(d, ignore_errors=True)
    print("[OK] 记忆保存：EBUSY 时回退覆盖写入，内容不丢、临时文件清理干净")


def test_event_loop_not_blocked():
    """_loop 必须只入队、不执行处理：否则 _call_api 等响应时读循环被阻塞必超时。

    这条正是"引用消息取不到 + 每条回复多等 10 秒"的根因回归测试。
    """
    import json as _json
    import main

    client = object.__new__(main.OneBotClient)
    client._pending = {}
    client._event_queue = asyncio.Queue()
    # 本用例要验证"处理器卡住时 _loop 仍能读"，所以把总超时放宽，别让它抢先取消
    client._event_timeout = 30.0

    release = asyncio.Event()
    handled = []

    async def slow_handler(data):
        await release.wait()          # 故意卡住，模拟长耗时处理
        handled.append(data)

    client._handle_event = slow_handler

    class FakeWS:
        def __init__(self, msgs):
            self._msgs = list(msgs)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._msgs:
                raise StopAsyncIteration
            return self._msgs.pop(0)

    class EchoPending:
        """记录 echo 响应是否被 _resolve_echo 处理到。"""
        def __init__(self):
            self.got = None

    client.ws = FakeWS([
        _json.dumps({"post_type": "message", "message": "事件1"}),
        _json.dumps({"status": "ok", "echo": "e1", "data": {"id": "x"}}),
        _json.dumps({"post_type": "message", "message": "事件2"}),
    ])

    async def scenario():
        loop_task = asyncio.create_task(client._loop())
        # 关键断言：处理器一直卡着，_loop 也必须在 1 秒内读完并退出
        await asyncio.wait_for(loop_task, timeout=1)
        # echo 响应不进队列，两个业务事件进队列
        assert client._event_queue.qsize() == 2, client._event_queue.qsize()

        worker = asyncio.create_task(client._event_worker())
        await asyncio.sleep(0.05)
        assert handled == [], "worker 应被 slow_handler 卡住，尚未处理完"
        release.set()
        await asyncio.sleep(0.1)
        worker.cancel()
        assert len(handled) == 2, handled

    asyncio.run(scenario())
    print("[OK] 事件队列：_loop 只入队不阻塞，echo 响应可被及时处理，业务仍串行")


def test_event_timeout_releases_worker():
    """单条事件卡死时必须被取消，不能拖住后面所有事件。

    超时之前 `await self._handle_event(data)` 没有上界：一条视频/大文档卡住，
    全 bot 所有会话就会无限排队（机制缺陷）。这里验证 timeouts.event_handle 生效。
    """
    import main

    client = object.__new__(main.OneBotClient)
    client._event_queue = asyncio.Queue()
    client._event_timeout = 0.2

    handled = []

    async def handler(data):
        if data.get("message_id") == 1:
            await asyncio.sleep(10)          # 模拟卡死的媒体处理
        handled.append(data.get("message_id"))

    client._handle_event = handler

    async def scenario():
        worker = asyncio.create_task(client._event_worker())
        await client._event_queue.put({"message_type": "private",
                                       "sender": {"user_id": 1}, "message_id": 1})
        await client._event_queue.put({"message_type": "private",
                                       "sender": {"user_id": 2}, "message_id": 2})
        await asyncio.sleep(0.6)
        # 第 1 条被放弃（不会进入 handled），第 2 条照常处理
        assert handled == [2], handled
        assert client._event_queue.qsize() == 0, client._event_queue.qsize()
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    print("[OK] 事件超时：卡死的那条被取消，worker 释放给后续事件")


def test_timeout_config():
    """timeouts 段：默认值、自定义值、非法值回落。"""
    import config

    cfg = config.load_config(Path(__file__).parent / "config.json")
    outer = config.timeout_of(cfg, "event_handle")
    assert outer > 0
    # 内层必须严格小于外层：否则内层永远轮不到触发（外层先掐掉），等于失效配置
    for key in ("onebot_api", "ffmpeg", "file_upload", "video_understand", "recover_reply"):
        inner = config.timeout_of(cfg, key)
        assert 0 < inner < outer, f"{key}={inner} 必须小于 event_handle={outer}"

    default = config.DEFAULTS["timeouts"]["event_handle"]
    assert config.timeout_of({"timeouts": {"event_handle": 900}}, "event_handle") == 900.0
    assert config.timeout_of({"timeouts": {"event_handle": "600s"}}, "event_handle") == default
    assert config.timeout_of({"timeouts": {"event_handle": 0}}, "event_handle") == default
    assert config.timeout_of({"timeouts": {"event_handle": None}}, "event_handle") == default
    print("[OK] 超时配置：内外层顺序、默认/自定义/非法值回落均正确")


def test_cq_string_media_parsing():
    """NapCat 以 CQ 码字符串返回消息时，媒体段也必须能解析出来。"""
    import main

    client = object.__new__(main.OneBotClient)
    client.bot_qq = "2024514917"

    raw = ("[CQ:reply,id=123][CQ:at,qq=2024514917]"
           "[CQ:image,file=a.jpg,url=https://cdn/a.jpg?x=1&amp;y=2]"
           "[CQ:video,file=v.mp4,url=https://cdn/v.mp4]"
           "[CQ:record,file=r.amr] 这是什么")

    text, at_bot, imgs, voices, vids = client._parse_message(raw)
    assert at_bot is True
    assert "这是什么" in text, text
    assert len(imgs) == 1 and imgs[0]["file"] == "a.jpg", imgs
    assert imgs[0]["url"] == "https://cdn/a.jpg?x=1&y=2", imgs[0]   # &amp; 已还原
    assert len(vids) == 1 and vids[0]["file"] == "v.mp4", vids
    assert len(voices) == 1 and voices[0]["file"] == "r.amr", voices

    print("[OK] CQ 码字符串解析：图片/视频/语音/@ 全部识别，HTML 实体已还原")


def test_download_size_limit():
    """媒体下载必须有体积上限：先看 Content-Length，边写边计数，超限中止。"""
    import http.server
    import threading
    from media import _download

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/big":
                self.send_response(200)
                self.send_header("Content-Length", str(500 * 1024 * 1024))  # 谎称 500MB
                self.end_headers()
                self.wfile.write(b"x" * 2048)
            else:
                data = b"hello" * 1000
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        # 正常小文件
        p = _download(f"http://127.0.0.1:{port}/small", ".bin", max_mb=1)
        assert Path(p).stat().st_size == 5000, "小文件应完整落盘"
        Path(p).unlink()

        # 声明 500MB > 1MB 上限 → 直接拒绝，且不留 .part 残file
        try:
            _download(f"http://127.0.0.1:{port}/big", ".bin", max_mb=1)
            raise AssertionError("超大文件必须被拒绝")
        except ValueError as exc:
            assert "过大" in str(exc), exc

        from media import _DOWNLOAD_DIR
        leftovers = list(_DOWNLOAD_DIR.glob("*.part"))
        assert not leftovers, f"中止后不应残留临时文件: {leftovers}"
    finally:
        srv.shutdown()
        srv.server_close()

    print("[OK] 媒体下载限流：超大文件提前拒绝、流式写盘、临时文件清理")


def test_extract_file_segs():
    """文件段提取：标准 file 段 / CQ 字符串 / JSON 卡片三种形态都要能识别。"""
    import json as _json
    import main

    ex = main.OneBotClient._extract_file_segs

    # 1. 标准 file 段
    segs = ex([{"type": "file", "data": {"file": "a.pdf", "file_id": "f1",
                                         "url": "https://x/a.pdf"}}])
    assert len(segs) == 1 and segs[0]["name"] == "a.pdf" and segs[0]["file_id"] == "f1", segs

    # 2. CQ 码字符串
    segs = ex("[CQ:reply,id=1][CQ:file,file=b.txt,file_id=f2,url=https://x/b.txt]")
    assert len(segs) == 1 and segs[0]["name"] == "b.txt" and segs[0]["file_id"] == "f2", segs

    # 3. JSON 卡片（QQ 文档类卡片用 qqdocurl 携带跳转链接）
    card = _json.dumps({"meta": {"detail_1": {"title": "报告.pdf",
                                              "qqdocurl": "https://docs.qq.com/x"}}})
    segs = ex([{"type": "json", "data": {"data": card}}])
    assert len(segs) == 1 and segs[0]["name"] == "报告.pdf", segs
    assert segs[0]["url"] == "https://docs.qq.com/x", segs[0]

    # 4. 普通消息不应误判
    assert ex([{"type": "text", "data": {"text": "hi"}}]) == []
    assert ex([{"type": "image", "data": {"file": "x.jpg"}}]) == []

    # 5. 卡片 JSON 损坏时安全跳过
    assert ex([{"type": "json", "data": {"data": "{not-json"}}]) == []

    print("[OK] 文件段提取：file 段 / CQ 码 / JSON 卡片都能识别，坏 JSON 安全跳过")


def test_resolve_file_seg():
    """消息自带的 file 段（私聊直接发文件）：按序尝试接口并成功下载。"""
    import http.server
    import threading
    import main

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            data = b"PDFDATA" * 200
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    client = object.__new__(main.OneBotClient)
    calls = []

    async def fake_api(action, params, timeout=10):
        calls.append(action)
        if action == "get_private_file_url":
            return {"status": "failed", "retcode": 100}      # 第一个接口不支持
        if action == "get_file":
            return {"status": "ok", "data": {"url": f"http://127.0.0.1:{port}/f"}}
        return {"status": "failed"}

    client._call_api = fake_api

    try:
        got = asyncio.run(client._resolve_file_seg(
            {"name": "报告.pdf", "file_id": "fid-1"}, "private", "111", ""))
    finally:
        srv.shutdown()
        srv.server_close()

    assert got and got[0] == "报告.pdf", got
    p = Path(got[1])
    assert p.exists() and p.stat().st_size == 1400, p
    assert calls == ["get_private_file_url", "get_file"], calls
    p.unlink()

    print("[OK] 消息 file 段：私聊按序回退到可用接口并成功下载")

    # 拿不到任何地址时应返回 None 而不是抛异常
    client2 = object.__new__(main.OneBotClient)

    async def all_fail(action, params, timeout=10):
        return {"status": "failed"}

    client2._call_api = all_fail
    assert asyncio.run(client2._resolve_file_seg(
        {"name": "x.bin", "file_id": "fid-x"}, "private", "111", "")) is None


def test_fetch_file_private_chain():
    """私聊文件（引用文件/离线通知，段里没有 url）的接口链必须只用官方接口。

    NapCat 没有 get_offline_file（go-cqhttp 才有），旧实现只在私聊分支调它，
    结果是引用文件必然拿不到、且只留一行 warning。这里回归覆盖：
    get_private_file_url 失败 → get_file 兜底成功。
    """
    import media
    from pathlib import Path

    p = Path(__file__).parent / "downloads" / "_t_fetch_file.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("hello", encoding="utf-8")
    calls = []

    async def fake_api(action, params):
        calls.append(action)
        if action == "get_private_file_url":
            return {"status": "failed", "retcode": 1404}
        if action == "get_file":
            return {"status": "ok", "data": {"file": str(p), "file_name": "_t_fetch_file.txt"}}
        return {"status": "failed"}

    try:
        got = asyncio.run(media.fetch_file(
            {"file_name": "_t_fetch_file.txt", "file_id": "fid-9"}, "private", fake_api))
    finally:
        p.unlink(missing_ok=True)

    assert got and got[0] == "_t_fetch_file.txt", got
    assert Path(got[1]) == p, got
    assert calls == ["get_private_file_url", "get_file"], calls
    assert "get_offline_file" not in calls, calls

    print("[OK] 私聊文件：get_private_file_url → get_file（不再调用不存在的 get_offline_file）")


def test_expand_forward():
    """合并转发：展开文本 + 图片/文件段 + 嵌套子记录，异常与深度都要有降级。"""
    import main

    client = object.__new__(main.OneBotClient)
    client.bot_qq = "2024514917"

    async def fake_api(action, params, timeout=10):
        assert action == "get_forward_msg", action
        fid = params["id"]
        if fid == "f1":
            return {"status": "ok", "data": {"messages": [
                {"sender": {"nickname": "张三"},
                 "message": [{"type": "text", "data": {"text": "第一句"}}]},
                {"sender": {"card": "李四(群名片)"},
                 "message": [{"type": "image", "data": {"file": "a.jpg"}}]},
                {"sender": {"nickname": "王五"},
                 "message": [{"type": "file", "data": {"file": "报告.pdf", "file_id": "fx"}}]},
                {"sender": {"nickname": "赵六"},
                 "message": [{"type": "forward", "data": {"id": "f2"}}]},
            ]}}
        if fid == "f2":
            return {"status": "ok", "data": {"messages": [
                {"sender": {"nickname": "孙七"},
                 "message": [{"type": "text", "data": {"text": "子记录内容"}}]},
            ]}}
        if fid == "bad":
            return {"status": "ok", "data": {}}
        raise RuntimeError("模拟接口失败")

    client._call_api = fake_api

    ex = asyncio.run(client._expand_forward("f1"))
    t = ex["text"]
    assert "共 4 条" in t, t
    assert "1. 张三：第一句" in t, t
    assert "[1张图片]" in t, t
    assert "[1个文件：报告.pdf]" in t, t
    assert "子记录内容" in t, "嵌套子转发应展开"
    assert len(ex["images"]) == 1 and ex["images"][0]["file"] == "a.jpg", ex["images"]
    assert len(ex["files"]) == 1 and ex["files"][0]["name"] == "报告.pdf", ex["files"]

    # 返回结构不含 messages → 明确提示而不是抛异常
    ex2 = asyncio.run(client._expand_forward("bad"))
    assert ("格式不支持" in ex2["text"] or "为空" in ex2["text"]), ex2["text"]

    # 接口异常 → 明确提示
    ex3 = asyncio.run(client._expand_forward("err"))
    assert "无法展开" in ex3["text"], ex3["text"]

    # 递归深度上限
    ex4 = asyncio.run(client._expand_forward("f2", depth=2))
    assert "嵌套过深" in ex4["text"], ex4["text"]

    # forward id 提取（数组与 CQ 字符串两种）
    ids = main.OneBotClient._extract_forward_ids(
        [{"type": "forward", "data": {"id": "abc"}}])
    assert ids == ["abc"], ids
    assert main.OneBotClient._extract_forward_ids("[CQ:forward,id=xyz]") == ["xyz"]

    print("[OK] 合并转发：文本/图片/文件/嵌套子记录均能展开，异常与深度有降级")


def test_llm_failure_notes():
    """LLM 超时 / 空 choices / 鉴权失败：必须返回一句能发出去的说明，而不是空串。"""
    import agent

    class FakeLLM:
        def __init__(self, exc=None, resp=None):
            self.exc, self.resp = exc, resp

        async def complete(self, messages, tools=None):
            if self.exc:
                raise self.exc
            return self.resp

    class FakeServices:
        openai_ready = True

    def run_with(exc=None, resp=None):
        a = agent.Agent(FakeLLM(exc, resp), FakeServices())
        return asyncio.run(a._tool_loop([], [], {"images": [], "voices": [], "files": []}))

    assert "超时" in run_with(exc=TimeoutError("timed out"))
    assert "没有返回内容" in run_with(resp={"choices": []})
    assert "鉴权" in run_with(exc=RuntimeError("401 Unauthorized"))
    assert "异常" in run_with(exc=RuntimeError("boom"))
    print("[OK] LLM 失败兜底：超时/空 choices/鉴权/其他异常均给出可发送的说明")


def test_tool_args_guard():
    """工具参数坏掉/缺必填项时：不执行工具，把原因回给模型让它重发。"""
    import agent
    import config

    # ① 解析层：合法参数原样通过（含网关直接给对象的情况）
    assert agent._parse_tool_args('{"query": "今天天气"}', "web_search") == (
        {"query": "今天天气"}, "")
    assert agent._parse_tool_args({"query": "x"}, "web_search") == ({"query": "x"}, "")

    # 被 max_tokens 截断：finish_reason=length 时明确指出截断
    args, why = agent._parse_tool_args('{"query": "今天天', "web_search", "length")
    assert args == {} and "不是合法 JSON" in why and "长度上限" in why, why
    # 没有 length 标记时不下这个结论
    args, why = agent._parse_tool_args('{"query": "今天天', "web_search")
    assert args == {} and "不是合法 JSON" in why and "长度上限" not in why, why

    # 合法 JSON 但不是对象
    for bad in ("null", "[]", "123", '"abc"'):
        args, why = agent._parse_tool_args(bad, "web_search")
        assert args == {} and "必须是 JSON 对象" in why, (bad, why)

    # 空参数 / 空字符串参数 → 必填校验拦住（这是原来静默传 {} 最坑的一种）
    for empty in ("", None, "{}", '{"query": "   "}'):
        args, why = agent._parse_tool_args(empty, "web_search")
        assert args == {} and "缺少必需参数 query" in why, (empty, why)

    # 类型异常（既不是 str 也不是 dict）
    args, why = agent._parse_tool_args(123, "web_search")
    assert args == {} and "类型异常" in why, why

    # ② 集成层：坏参数不调用工具，但必须回一条 tool 消息，下一轮模型据此收尾
    calls = []

    class FakeLLM:
        def __init__(self):
            self.rounds = []

        async def complete(self, messages, tools=None):
            self.rounds.append([dict(m) for m in messages])
            if len(self.rounds) == 1:
                return {"choices": [{
                    "finish_reason": "length",
                    "message": {
                        "role": "assistant", "content": "",
                        "tool_calls": [{
                            "id": "call_1", "type": "function",
                            "function": {"name": "web_search",
                                         "arguments": '{"query": "今天天'}}]}}]}
            return {"choices": [{"finish_reason": "stop", "message": {
                "role": "assistant", "content": "我重新查一下。"}}]}

    class FakeServices:
        openai_ready = True

    a = agent.Agent(FakeLLM(), FakeServices())

    async def fake_exec(name, args, outputs, entry=None, call_api=None):
        calls.append((name, args))
        return "不该走到这里"

    a._exec_tool = fake_exec
    out = asyncio.run(a._tool_loop([], [], {"images": [], "voices": [], "files": []}))
    assert out == "我重新查一下。", out
    assert calls == [], f"参数坏了却执行了工具: {calls}"
    tool_msgs = [m for m in a.llm.rounds[1] if m.get("role") == "tool"]
    assert tool_msgs and "本次未执行" in tool_msgs[0]["content"], tool_msgs
    # 坏 JSON 的原文片段必须回给模型，否则它不知道从哪儿断的
    assert '{"query"' in tool_msgs[0]["content"], tool_msgs
    assert tool_msgs[0]["tool_call_id"] == "call_1", tool_msgs
    # 每个 tool_call 都必须有对应的 tool 回复，否则下一轮请求结构不合法
    assert len(tool_msgs) == 1

    # ③ 上限配置：max_tokens 与 max_reply_length 同步（两个数要一起动）
    cfg = config.load_config(Path(__file__).parent / "config.json")
    assert cfg["openai"]["max_tokens"] == 4000, cfg["openai"]["max_tokens"]
    assert cfg["reply_rules"]["max_reply_length"] == 4000, \
        cfg["reply_rules"]["max_reply_length"]
    print("[OK] 工具参数守卫：坏 JSON/非对象/缺必填均不执行并回因，"
          "max_tokens 与 max_reply_length 同步为 4000")


def test_tool_call_id_backfill():
    """上游漏 tool_call.id：本地补号，保证 assistant 与 tool 消息能配对。"""
    import agent

    # ① 就地补号：缺的补、空串也算缺、已有的不动
    tcs = [{"function": {"name": "web_search"}}, {"id": "call_x", "function": {}}]
    agent._ensure_tool_call_ids(tcs, 2)
    assert tcs[0]["id"] == "call_local_2_0", tcs
    assert tcs[1]["id"] == "call_x", tcs
    tcs = [{"id": "", "function": {}}]
    agent._ensure_tool_call_ids(tcs, 0)
    assert tcs[0]["id"] == "call_local_0_0", tcs
    agent._ensure_tool_call_ids(tcs, 0)      # 幂等：再跑一次不该改
    assert tcs[0]["id"] == "call_local_0_0", tcs

    # ② 集成层：无 id 的 tool_call 仍能执行，且两条消息 id 一致
    executed = []

    class FakeLLM:
        def __init__(self):
            self.rounds = []

        async def complete(self, messages, tools=None):
            self.rounds.append([dict(m) for m in messages])
            if len(self.rounds) == 1:
                return {"choices": [{"finish_reason": "tool_calls", "message": {
                    "role": "assistant", "content": "",
                    "tool_calls": [{"type": "function", "function": {
                        "name": "web_search",
                        "arguments": '{"query": "量子计算"}'}}]}}]}
            return {"choices": [{"finish_reason": "stop", "message": {
                "role": "assistant", "content": "查到了。"}}]}

    class FakeServices:
        openai_ready = True

    a = agent.Agent(FakeLLM(), FakeServices())

    async def fake_exec(name, args, outputs, entry=None, call_api=None):
        executed.append((name, args))
        return "搜索结果：…"

    a._exec_tool = fake_exec
    out = asyncio.run(a._tool_loop([], [], {"images": [], "voices": [], "files": []}))
    assert out == "查到了。", out
    assert executed == [("web_search", {"query": "量子计算"})], executed

    msgs = a.llm.rounds[1]
    asst = [m for m in msgs if m.get("role") == "assistant"][-1]
    tool = [m for m in msgs if m.get("role") == "tool"][0]
    assert asst["tool_calls"][0]["id"] == "call_local_0_0", asst
    # 两边必须是同一个 id，否则上游会判消息结构不合法（400）
    assert tool["tool_call_id"] == asst["tool_calls"][0]["id"], (tool, asst)
    print("[OK] tool_call 缺 id：本地补号，assistant 与 tool 消息配对一致")


def test_recover_reply():
    """处理中途失败：带着已有上下文补救一次；补救也失败才退回固定文案。"""
    import agent as ag
    import services as svc

    # ① 异常 → 人话，不回显原始串（URL/key 不能进提示词）
    assert ag._reason(TimeoutError("timed out")) == "等待超时"
    assert ag._reason(svc.ServiceUnavailable("多模态推理失败: HTTP Error 401")) == "多模态推理失败"
    assert ag._reason(OSError("no space left")) == "文件或网络读写失败"
    assert ag._reason(KeyError("choices")) == "消息内容格式异常"
    assert "http" not in ag._reason(RuntimeError("boom http://x/y?key=1"))

    class FakeLLM:
        system_prompt = "你是助手"

        def __init__(self, content="好的", boom=None):
            self.content, self.boom = content, boom
            self.calls = []

        async def complete(self, messages, tools=None):
            self.calls.append(list(messages))
            if self.boom:
                raise self.boom
            return {"choices": [{"message": {"role": "assistant", "content": self.content}}]}

        async def maybe_summarize(self, *a, **kw):
            return None

    class FakeServices:
        openai_ready = True

    # ② 补救成功：提示词里带失败原因，且不带工具（不二次踩坑）
    a = ag.Agent(FakeLLM("我先说能确定的：图我看到了。"), FakeServices())
    reply = asyncio.run(a._recover_reply(
        {"summary": "", "recent": []}, "你好", TimeoutError("timed out")))
    assert reply == "我先说能确定的：图我看到了。", reply
    note = a.llm.calls[0][-1]
    assert note["role"] == "system" and "本轮处理中途失败" in note["content"], note
    assert "等待超时" in note["content"], note

    # ③ 补救失败 / 空内容 → None
    a2 = ag.Agent(FakeLLM(boom=RuntimeError("401")), FakeServices())
    assert asyncio.run(a2._recover_reply({"recent": []}, "你好", ValueError("x"))) is None
    a3 = ag.Agent(FakeLLM("   "), FakeServices())
    assert asyncio.run(a3._recover_reply({"recent": []}, "你好", ValueError("x"))) is None

    # ④ 超时取值：非法值回落 60 秒
    assert ag.Agent(FakeLLM(), FakeServices(), recover_timeout=0).recover_timeout == 60.0
    assert ag.Agent(FakeLLM(), FakeServices(), recover_timeout=-5).recover_timeout == 60.0

    # ⑤ 归一化整体失败时的降级文本
    txt = a._fallback_user_text({"text": "这是什么", "images": [1, 2], "videos": [3]},
                                TimeoutError("timed out"))
    assert "这是什么" in txt and "图片×2" in txt and "视频×1" in txt, txt
    assert "等待超时" in txt, txt
    assert a._fallback_user_text({"text": ""}, ValueError("x")) == ""

    # ⑥ 端到端：工具循环炸了 → 补救成功 → 正常写记忆并返回可发送的文本
    async def fake_norm(user_input, call_api, entry=None):
        return "帮我看看这张图"

    async def boom_loop(*args, **kwargs):
        raise RuntimeError("模拟工具层崩溃")

    a4 = ag.Agent(FakeLLM("补救回复"), FakeServices())
    a4._normalize, a4._tool_loop = fake_norm, boom_loop
    ctx = {}
    segs = asyncio.run(a4.handle("s1", {"text": "帮我看看这张图"}, ctx,
                                 {"mode": "accumulate"}, None))
    assert segs[0]["data"]["text"] == "补救回复", segs
    recent = ctx["s1"]["recent"]
    assert "帮我看看这张图" in recent[-2]["content"], recent
    assert recent[-1]["content"] == "补救回复", recent

    # ⑦ 补救也失败 → 原样抛出原始异常，交给 main 的固定文案
    a5 = ag.Agent(FakeLLM(boom=RuntimeError("接口又炸了")), FakeServices())
    a5._normalize, a5._tool_loop = fake_norm, boom_loop
    try:
        asyncio.run(a5.handle("s2", {"text": "hi"}, {}, {"mode": "accumulate"}, None))
    except RuntimeError as exc:
        assert "模拟工具层崩溃" in str(exc), exc
    else:
        raise AssertionError("补救失败时应原样抛出原始异常")

    # ⑧ 逐份媒体：原生异常（不是 ServiceUnavailable）也降级进 prompt
    class BadImgServices(FakeServices):
        audio_model = ""
        voice_ready = False
        video_ready = False

        async def describe_image(self, *a, **kw):
            raise OSError("disk full")

    a6 = ag.Agent(FakeLLM(), BadImgServices())
    text6 = asyncio.run(a6._normalize({"images": ["/tmp/x.jpg"]}, None,
                                     {"summary": "", "recent": []}))
    assert "视觉理解失败" in text6 and "文件或网络读写失败" in text6, text6
    print("[OK] 补救回复：失败原因回给模型、失败/空内容退回固定文案、"
          "媒体原生异常降级进 prompt")


def test_failure_notify():
    """超时/异常兜底：给对方一句说明，并把这轮写进会话记忆。"""
    import config as cfgmod
    import main

    client = object.__new__(main.OneBotClient)
    client.config = cfgmod.load_config(Path(__file__).parent / "config.json")
    client.bot_qq = "2024514917"
    client._event_timeout = 1200
    client.context = {}
    client._mem_dirty = False
    sent = []

    async def fake_send(msg_type, user_id, group_id, segments):
        sent.append((msg_type, user_id, group_id, segments))

    client._send_segments = fake_send

    private_evt = {
        "post_type": "message", "message_type": "private",
        "sender": {"user_id": 10001},
        "message": [{"type": "text", "data": {"text": "帮我看看这个视频"}}],
    }
    asyncio.run(client._notify_failure(private_evt, "timeout"))
    assert len(sent) == 1, sent
    mt, uid, gid, segs = sent[0]
    assert (mt, uid, gid) == ("private", "10001", ""), sent[0]
    note = segs[0]["data"]["text"]
    assert "1200" in note and "放弃" in note, note

    entry = client.context["p10001"]
    assert len(entry["recent"]) == 2, entry
    assert entry["recent"][0]["role"] == "user"
    assert "帮我看看这个视频" in entry["recent"][0]["content"], entry["recent"][0]
    assert entry["recent"][1] == {"role": "assistant", "content": note}, entry["recent"]
    assert client._mem_dirty is True

    # 同一事件重复兜底：只发消息，不重复记账（避免上下文里出现两份一样的失败记录）
    asyncio.run(client._notify_failure(private_evt, "timeout"))
    assert len(entry["recent"]) == 2, entry

    # 群聊 + 异常分支：文案不同，媒体类型要体现在记账文本里
    group_evt = {
        "post_type": "message", "message_type": "group", "group_id": 555,
        "sender": {"user_id": 20002},
        "message": [{"type": "image", "data": {"file": "x"}}],
    }
    asyncio.run(client._notify_failure(group_evt, "error"))
    assert len(sent) == 3, sent
    assert sent[-1][0] == "group" and sent[-1][2] == "555", sent[-1]
    assert "出错" in sent[-1][3][0]["data"]["text"], sent[-1]
    g_entry = client.context["g555"]["recent"]
    assert "[图片×1]" in g_entry[0]["content"], g_entry

    # meta_event / 无关通知不该打扰
    before = len(sent)
    asyncio.run(client._notify_failure({"post_type": "meta_event"}, "timeout"))
    asyncio.run(client._notify_failure(
        {"post_type": "notice", "notice_type": "group_recall"}, "error"))
    assert len(sent) == before, sent

    print("[OK] 失败兜底：超时/异常均回复说明并写入上下文，无关事件不打扰")


if __name__ == "__main__":
    test_read_text_like()
    test_read_office_docx()
    test_read_office_xlsx()
    test_read_office_pptx()
    test_read_pdf_best_effort()
    test_extension_routing()
    test_strip_html()
    asyncio.run(test_fetch_url_graceful())
    test_search_parsers()
    test_reply_reference()
    test_extract_file_segs()
    test_resolve_file_seg()
    test_fetch_file_private_chain()
    test_expand_forward()
    test_cq_string_media_parsing()
    test_env_override_ws_url()
    test_memory_save_busy_fallback()
    test_event_loop_not_blocked()
    test_event_timeout_releases_worker()
    test_timeout_config()
    test_llm_failure_notes()
    test_tool_args_guard()
    test_tool_call_id_backfill()
    test_recover_reply()
    test_failure_notify()
    test_download_size_limit()
    print("\n全部离线测试通过 ✅")
