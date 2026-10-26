"""真实端到端验证：用实际 Services 类 + 真实 config.json，真调音频/文档理解。
运行后输出实际模型返回，用于确认集成代码（不仅是裸 API）可用。
"""
import asyncio
import json
import math
import struct
import tempfile
import wave
from pathlib import Path

from services import Services


def make_wav(path: str):
    sr, dur, freq = 8000, 1.0, 440
    n = int(sr * dur)
    w = wave.open(path, "wb")
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
    w.writeframes(b"".join(struct.pack("<h", int(32767 * 0.3 * math.sin(2 * math.pi * freq * i / sr)))
                           for i in range(n)))
    w.close()


def build_pdf(path: str, text: str):
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
    ]
    stream = f"BT /F1 20 Tf 72 700 Td ({text}) Tj ET".encode("latin-1")
    objs.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    pdf = b"%PDF-1.4\n"; offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(pdf)); pdf += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(pdf); n = len(objs) + 1
    pdf += f"xref\n0 {n}\n".encode() + b"0000000000 65535 f \n"
    for off in offs:
        pdf += f"{off:010d} 00000 n \n".encode()
    pdf += b"trailer\n<< /Size " + str(n).encode() + b" /Root 1 0 R >>\n"
    pdf += b"startxref\n" + str(xref).encode() + b"\n%%EOF"
    Path(path).write_bytes(pdf)


def main():
    cfg = json.loads(Path("config.json").read_text(encoding="utf-8"))
    s = Services(cfg)
    print("audio_model =", s.audio_model, "| audio_ready =", s.audio_ready)
    print("document_model =", s.document_model, "| document_ready =", s.document_ready)

    d = tempfile.mkdtemp()
    wav = str(Path(d) / "beep.wav"); make_wav(wav)
    pdf = str(Path(d) / "doc.pdf"); build_pdf(pdf, "End to end capability verification document.")

    print("\n== understand_audio(本地 wav) ==")
    try:
        print(asyncio.run(s.understand_audio(wav)))
    except Exception as e:
        print("AUDIO ERROR:", type(e).__name__, e)

    print("\n== understand_document(本地 pdf，走 Files API) ==")
    try:
        print(asyncio.run(s.understand_document(pdf)))
    except Exception as e:
        print("DOC ERROR:", type(e).__name__, e)


if __name__ == "__main__":
    main()
