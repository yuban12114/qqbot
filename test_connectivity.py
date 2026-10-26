"""一次性连通性测试：直接调用项目里真实的 Services / LLMClient 类，
逐项验证 config.json 中的 LLM 大脑与各 services 能否真正联通并正常工作。
不修改任何业务代码，仅做只读式探测。"""
import asyncio
import importlib.util
from pathlib import Path

HERE = Path(__file__).parent

# ---- 动态导入项目模块（与运行目录无关）----
def _load(mod_name, fname):
    spec = importlib.util.spec_from_file_location(mod_name, HERE / fname)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

cfg_mod = _load("config_mod", "config.py")
svc_mod = _load("services_mod", "services.py")
ai_mod = _load("ai_mod", "ai_client.py")

cfg = cfg_mod.load_config(HERE / "config.json")
svc = svc_mod.Services(cfg)
llm = ai_mod.LLMClient(cfg["openai"])

# 测试用本地图片（ffmpeg 生成纯色图，无需 PIL）
TEST_IMG = HERE / "cache" / "test_image.png"

def log(title, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {title}")
    if detail:
        print("       " + detail.replace("\n", "\n       "))
    print()

async def test_llm_brain():
    title = f"LLM 大脑 (DeepSeek, model={llm.model})"
    try:
        data = await llm.complete([
            {"role": "system", "content": "你是测试助手，只回复一个字：ok"},
            {"role": "user", "content": "ping"},
        ])
        msg = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        log(title, bool(msg), f"回复: {msg!r} | usage={usage}")
    except Exception as e:
        log(title, False, f"{type(e).__name__}: {e}")

async def test_vision():
    title = f"视觉理解 (Ark vision, model={svc.vision_model})"
    try:
        if not TEST_IMG.exists():
            raise RuntimeError("测试图片未生成")
        desc = await asyncio.wait_for(
            svc.describe_image(str(TEST_IMG), "用一句话描述这张图片的颜色和主体。"),
            timeout=90,
        )
        log(title, bool(desc), f"描述: {desc!r}")
    except Exception as e:
        log(title, False, f"{type(e).__name__}: {e}")

async def test_image_gen():
    title = f"文生图 (Ark image, model={svc.image_model}, size={svc.image_size}, watermark={svc.image_watermark})"
    try:
        out = await asyncio.wait_for(
            svc.generate_image("一只蓝色的小鲸鱼，卡通风格，简洁背景"),
            timeout=120,
        )
        p = Path(out)
        ok = p.exists() and p.stat().st_size > 1000
        log(title, ok, f"输出文件: {out} ({p.stat().st_size if p.exists() else 0} bytes)")
    except Exception as e:
        log(title, False, f"{type(e).__name__}: {e}")

async def test_tts():
    title = f"语音合成 TTS (openspeech, voice={svc.tts_voice}, resource={svc.tts_resource_id})"
    try:
        out = await asyncio.wait_for(
            svc.text_to_speech("你好，这是一条语音合成测试。"),
            timeout=60,
        )
        p = Path(out)
        ok = p.exists() and p.stat().st_size > 200
        log(title, ok, f"输出文件: {out} ({p.stat().st_size if p.exists() else 0} bytes)")
    except Exception as e:
        log(title, False, f"{type(e).__name__}: {e}")

async def test_asr():
    title = f"语音识别 ASR (openspeech auc, resource={svc.asr_resource_id})"
    try:
        # 用 ffmpeg 生成一段静音 wav，转码后提交；验证端点+鉴权+提交/查询流程连通。
        # 静音不会返回文字（code=20000003 静音），但能证明连接与协议正确。
        wav = HERE / "cache" / "test_asr.wav"
        ffmpeg = __import__("shutil").which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("shutil.which('ffmpeg') 找不到 ffmpeg —— 请将其目录加入 PATH")
        import subprocess
        subprocess.run(
            [ffmpeg, "-y", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
             "-t", "1", str(wav)],
            check=True, capture_output=True, timeout=30,
        )
        txt = await asyncio.wait_for(svc.speech_to_text(str(wav)), timeout=110)
        # 静音返回空字符串属正常；只要提交+查询流程无 401/403 即算连通
        log(title, True, f"端点/鉴权/提交流程成功，识别结果(静音应为空): {txt!r}")
    except Exception as e:
        log(title, False, f"{type(e).__name__}: {e}")

async def test_web_search():
    title = "联网搜索 (DuckDuckGo lite, 无需 key)"
    try:
        res = await asyncio.wait_for(svc.web_search("今天北京天气"), timeout=30)
        ok = bool(res) and "失败" not in res
        log(title, ok, f"结果片段: {res[:120]!r}")
    except Exception as e:
        log(title, False, f"{type(e).__name__}: {e}")

async def main():
    print("=" * 72)
    print("config.json 连通性测试（真实调用项目代码）")
    print("=" * 72)
    print(f"ffmpeg 发现: {__import__('shutil').which('ffmpeg')}")
    print(f"Ark api_key 前缀: {svc.api_key[:12]}... | voice_api_key 前缀: {svc.voice_api_key[:12]}...")
    print()

    # 准备测试图片
    ffmpeg = __import__("shutil").which("ffmpeg")
    if ffmpeg:
        import subprocess
        TEST_IMG.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x200",
             "-frames:v", "1", str(TEST_IMG)],
            check=True, capture_output=True, timeout=30,
        )

    await test_llm_brain()
    await test_vision()
    await test_image_gen()
    await test_tts()
    await test_asr()
    await test_web_search()
    print("=" * 72)
    print("测试结束。")

if __name__ == "__main__":
    asyncio.run(main())
