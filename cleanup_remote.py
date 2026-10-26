"""查看 / 清理火山方舟 Files API 上的远端文件。

官方说明（2026-10 文档）：
  · 文件默认保留 **7 天**后自动过期清理（expire_at 可设 1~30 天）
  · 总存储容量 **20 GB**；512 MB 只是**单文件**上限（视频走 TOS 可达 2 GB）
  · 因此正常使用不需要频繁清理；保留 file_id 还能让多轮追问复用，无需重复上传

用法：
  cleanup_remote.bat                 Windows 双击（先看用量，确认后清理）
  python cleanup_remote.py --stats   只统计用量，不删除
  python cleanup_remote.py           查看用量 → 确认 → 全量清理
"""
import json
import ssl
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CAPACITY_GB = 20.0
GB = 1024 ** 3


def load_service_cfg() -> dict:
    p = ROOT / "config.json"
    if not p.exists():
        print(f"[错误] 找不到配置文件：{p}")
        sys.exit(1)
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[错误] config.json 解析失败：{exc}")
        sys.exit(1)
    o = (cfg.get("services") or {}).get("openai") or {}
    if not o.get("api_key") or not o.get("base_url"):
        print("[错误] config.json 缺少 services.openai.api_key 或 base_url")
        sys.exit(1)
    return o


def main() -> None:
    stats_only = any(a in ("--stats", "-s") for a in sys.argv[1:])
    o = load_service_cfg()
    key = str(o["api_key"]).strip()
    base = str(o["base_url"]).rstrip("/")
    ctx = ssl.create_default_context()

    def call(method: str, path: str):
        req = urllib.request.Request(f"{base}/{path}", method=method)
        req.add_header("Authorization", f"Bearer {key}")
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            body = resp.read()
        return json.loads(body) if body else {}

    # ---------- 1) 拉取全部文件（after 游标 + id 去重，兼容不支持游标的实现） ----------
    files, seen, after = [], set(), None
    for _ in range(50):
        q = "files?limit=100" + (f"&after={after}" if after else "")
        try:
            info = call("GET", q) or {}
        except Exception as exc:
            print(f"[错误] 查询文件列表失败：{exc}")
            break
        items = info.get("data") or []
        fresh = [f for f in items if f.get("id") and f["id"] not in seen]
        if not fresh:
            break
        for f in fresh:
            seen.add(f["id"])
            files.append(f)
        after = fresh[-1]["id"]
        if len(items) < 100:
            break

    total_bytes = sum(int(f.get("bytes") or 0) for f in files)
    gb = total_bytes / GB
    pct = gb / CAPACITY_GB * 100

    print(f"目标：{base}")
    print("-" * 52)
    print(f"当前占用：{len(files)} 个文件，{gb:.3f} GB / {CAPACITY_GB:.0f} GB（{pct:.1f}%）")
    if pct >= 80:
        print("⚠ 已超过 80%，建议清理")
    elif pct >= 50:
        print("· 已过半，可择机清理")
    else:
        print("· 用量健康（文件 7 天后自动过期，无需频繁清理）")

    if stats_only:
        print("-" * 52)
        print("（--stats 只统计，未删除任何文件）")
        return

    if not files:
        print("-" * 52)
        print("没有可清理的文件。")
        return

    # ---------- 2) 逐个删除 ----------
    print("-" * 52)
    deleted, freed, failed = 0, 0, 0
    for f in files:
        try:
            call("DELETE", f"files/{f['id']}")
            deleted += 1
            freed += int(f.get("bytes") or 0)
        except Exception as exc:
            failed += 1
            print(f"  删除失败 {f.get('id')}: {str(exc)[:80]}")

    print(f"完成：删除 {deleted} 个文件，释放 {freed / GB:.3f} GB"
          + (f"，失败 {failed} 个" if failed else ""))
    print("提示：删除后此前的 file_id 失效，正在进行的多轮追问需要重新上传。")


if __name__ == "__main__":
    main()
