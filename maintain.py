"""持久化数据维护工具：查看占用 + **选择性**清理 + 初始化修复。

与 cleanup_remote.py（只管方舟远端文件）不同，这里覆盖**本机全部持久化数据**，
并且每一项都可以单独勾选，不会一键清空。

用法：
    python maintain.py                 交互式菜单（可多选）
    python maintain.py --stats          只查看占用，不改动任何东西
    python maintain.py 1 3              直接执行编号 1 和 3（仍需确认）
    python maintain.py 1 3 -y           同上但跳过确认（谨慎）
    python maintain.py --init           只做初始化 / 修复

可选项：
    1  会话记忆 memory.json         （可再选：全部 / 单个会话 / 仅媒体索引表）
    2  下载目录 downloads/
    3  缓存目录 cache/
    4  系统临时残留 qqbot-*
    5  方舟远端文件（Files API）    （转调 cleanup_remote.py）
    6  容器日志（docker 日志截断）
    7  初始化 / 修复（建目录、补空 memory.json、校验配置）

安全约定：
  · 清空记忆前会自动备份到 <记忆文件>.bak-<时间戳>，删错可找回
  · 其余目录只删内容、不删目录本身（避免 Docker 把挂载点建成文件）
  · NapCat 登录态（napcat/qq、napcat/config）**不在清单里**——清了要重新扫码
"""
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:  # Windows 控制台默认 GBK，中文会乱码
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parent
GB = 1024 ** 3

ITEMS = [
    (1, "会话记忆 memory.json", "对话历史 + 媒体索引表（可再细分）"),
    (2, "下载目录 downloads/", "QQ 收到的图片/语音/视频/文件"),
    (3, "缓存目录 cache/", "上传临时文件、生成图、TTS、HTML 缓存"),
    (4, "系统临时残留 qqbot-*", "yt-dlp 失败时留下的工作目录"),
    (5, "方舟远端文件", "Files API 上的已上传媒体（转调 cleanup_remote.py）"),
    (6, "容器日志", "docker 日志截断（需要 docker 命令）"),
    (7, "初始化 / 修复", "建目录、补空 memory.json、校验 config.json"),
]


# ------------------------------------------------------------------ 基础
def h(n: float) -> str:
    """字节数转人类可读。"""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def load_cfg() -> dict:
    p = ROOT / "config.json"
    if not p.exists():
        print(f"[错误] 找不到配置文件：{p}")
        sys.exit(1)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"[错误] config.json 解析失败：{exc}")
        sys.exit(1)


def mem_path(cfg: dict) -> Path:
    """记忆文件路径：取 memory.persistence.path（相对则基于项目根）。"""
    m = (cfg.get("memory") or {}).get("persistence") or {}
    p = Path(m.get("path") or "memory.json")
    return p if p.is_absolute() else ROOT / p


def cache_dir(cfg: dict) -> Path:
    svc = (cfg.get("services") or {}).get("openai") or {}
    return ROOT / (svc.get("cache_dir") or "cache")


def dir_stats(d: Path) -> tuple[int, int]:
    """返回 (文件数, 总字节)。目录不存在则 (0, 0)。"""
    if not d.exists():
        return 0, 0
    cnt = size = 0
    try:
        for f in d.rglob("*"):
            if f.is_file():
                try:
                    cnt += 1
                    size += f.stat().st_size
                except OSError:
                    pass
    except Exception:  # noqa: BLE001
        pass
    return cnt, size


def clear_dir(d: Path) -> tuple[int, int]:
    """清空目录内容（保留目录本身），返回 (删除数, 释放字节)。"""
    cnt = freed = 0
    if not d.exists():
        return 0, 0
    try:
        for f in d.iterdir():
            try:
                if f.is_file():
                    sz = f.stat().st_size
                    f.unlink(missing_ok=True)
                    cnt += 1
                    freed += sz
                elif f.is_dir():
                    sz = sum(x.stat().st_size for x in f.rglob("*") if x.is_file())
                    shutil.rmtree(f, ignore_errors=True)
                    cnt += 1
                    freed += sz
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    return cnt, freed


# ------------------------------------------------------------------ 统计
def memory_stats(p: Path) -> tuple[int, int, int]:
    """返回 (会话数, 消息条数, 媒体索引条数)。"""
    if not p.exists():
        return 0, 0, 0
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return -1, 0, 0
    if not isinstance(data, dict):
        return -1, 0, 0
    sessions = len(data)
    msgs = sum(len(v.get("recent") or []) for v in data.values()
               if isinstance(v, dict))
    media = sum(len(v.get("media") or {}) for v in data.values()
                if isinstance(v, dict))
    return sessions, msgs, media


def remote_stats() -> None:
    """转调 cleanup_remote.py --stats，只看远端用量。"""
    script = ROOT / "cleanup_remote.py"
    if not script.exists():
        print("  （未找到 cleanup_remote.py，跳过远端统计）")
        return
    try:
        subprocess.run([sys.executable, str(script), "--stats"], timeout=120)
    except Exception as exc:  # noqa: BLE001
        print(f"  远端统计失败：{exc}")


def show_stats(cfg: dict) -> None:
    print("=" * 56)
    print("  持久化数据占用")
    print("=" * 56)
    mp = mem_path(cfg)
    s, m, md = memory_stats(mp)
    state = "（文件损坏或为空）" if s < 0 else f"{s} 个会话 / {m} 条消息 / {md} 条媒体索引"
    print(f"  [1] 记忆 {mp.name:<16} {state}")
    for no, d in ((2, ROOT / "downloads"), (3, cache_dir(cfg))):
        c, sz = dir_stats(d)
        print(f"  [{no}] {d.name + '/':<18} {c} 个文件，{h(sz)}")
    tmp_root = Path(tempfile.gettempdir())
    tmp = list(tmp_root.glob("qqbot-*"))
    print(f"  [4] 系统临时 qqbot-*     {len(tmp)} 项")
    print(f"  [5] 方舟远端             需联网查询（见下）")
    print(f"  [6] 容器日志             需 docker 命令")
    print(f"  [7] 初始化 / 修复        建目录 + 校验配置")
    print("-" * 56)
    try:
        remote_stats()
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ 各项动作
def act_memory(cfg: dict, auto_yes: bool = False) -> None:
    p = mem_path(cfg)
    if not p.exists():
        print(f"  记忆文件不存在：{p}（可用选项 7 初始化）")
        return
    s, m, md = memory_stats(p)
    print(f"  当前：{s} 个会话 / {m} 条消息 / {md} 条媒体索引")
    print("    1) 清空全部（写回 {}）")
    print("    2) 删除某一个会话")
    print("    3) 仅清空媒体索引表（保留对话文本）")
    sub = input("  选择 [1/2/3，回车取消]: ").strip()

    if sub == "1":
        if not _confirm("  确认清空全部记忆？", auto_yes):
            return
        _backup(p)
        p.write_text("{}", encoding="utf-8")
        print(f"  已清空：{p}（备份见 {p.name}.bak-*）")
    elif sub == "2":
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"  解析失败：{exc}")
            return
        keys = list(data)
        if not keys:
            print("  没有会话可删。")
            return
        for i, k in enumerate(keys, 1):
            v = data[k] if isinstance(data[k], dict) else {}
            print(f"    {i}. {k}（{len(v.get('recent') or [])} 条消息，"
                  f"{len(v.get('media') or {})} 条媒体索引）")
        pick = input("  输入序号，回车取消: ").strip()
        if not pick.isdigit() or not (1 <= int(pick) <= len(keys)):
            print("  已取消。")
            return
        key = keys[int(pick) - 1]
        if not _confirm(f"  确认删除会话 {key}？", auto_yes):
            return
        _backup(p)
        data.pop(key, None)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                     encoding="utf-8")
        print(f"  已删除会话：{key}")
    elif sub == "3":
        if not _confirm("  确认清空所有会话的媒体索引表？（对话文本保留）", auto_yes):
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"  解析失败：{exc}")
            return
        n = 0
        for v in data.values():
            # media_seq 故意不清：编号"永不复用"——recent 文本里还留着 [图片 m1] 标记，
            # 若计数器归零，新图片会再次分到 m1，与历史标记指向不同资源。
            if isinstance(v, dict) and v.pop("media", None):
                n += 1
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                     encoding="utf-8")
        print(f"  已清理 {n} 个会话的媒体索引（对话文本未动）")
    else:
        print("  已取消。")


def act_downloads(auto_yes: bool = False) -> None:
    d = ROOT / "downloads"
    c, sz = dir_stats(d)
    if not c:
        print("  downloads/ 为空。")
        return
    if not _confirm(f"  确认清空 downloads/（{c} 个文件，{h(sz)}）？", auto_yes):
        return
    n, freed = clear_dir(d)
    print(f"  已删除 {n} 项，释放 {h(freed)}")


def act_cache(cfg: dict, auto_yes: bool = False) -> None:
    d = cache_dir(cfg)
    c, sz = dir_stats(d)
    if not c:
        print(f"  {d.name}/ 为空。")
        return
    if not _confirm(f"  确认清空 {d.name}/（{c} 个文件，{h(sz)}）？", auto_yes):
        return
    n, freed = clear_dir(d)
    print(f"  已删除 {n} 项，释放 {h(freed)}")


def act_tmp(auto_yes: bool = False) -> None:
    tmp_root = Path(tempfile.gettempdir())
    items = list(tmp_root.glob("qqbot-*"))
    if not items:
        print("  没有 qqbot-* 残留。")
        return
    if not _confirm(f"  确认清除 {len(items)} 项系统临时残留？", auto_yes):
        return
    n = 0
    for f in items:
        try:
            if f.is_dir():
                shutil.rmtree(f, ignore_errors=True)
            else:
                f.unlink(missing_ok=True)
            n += 1
        except Exception:  # noqa: BLE001
            pass
    print(f"  已清除 {n} 项（目录：{tmp_root}）")


def act_remote(auto_yes: bool = False) -> None:
    script = ROOT / "cleanup_remote.py"
    if not script.exists():
        print("  未找到 cleanup_remote.py，跳过。")
        return
    print("  提示：删除后此前的 file_id 失效，正在进行的多轮追问需重新上传。")
    if not _confirm("  确认清理方舟远端全部文件？", auto_yes):
        return
    try:
        subprocess.run([sys.executable, str(script)], timeout=600)
    except Exception as exc:  # noqa: BLE001
        print(f"  远端清理失败：{exc}")


def act_logs(auto_yes: bool = False) -> None:
    names = ["qq-ai-bot", "napcat"]
    if not _confirm(f"  确认截断容器日志（{', '.join(names)}）？", auto_yes):
        return
    done = 0
    for n in names:
        try:
            r = subprocess.run(
                ["docker", "inspect", "--format", "{{.LogPath}}", n],
                capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                print(f"  {n}: 未找到容器或 docker 不可用，跳过")
                continue
            lp = r.stdout.strip()
            if not lp:
                continue
            # 原地截断，不删文件（删了 Docker 会继续往已删除的 inode 写）
            with open(lp, "w"):
                pass
            print(f"  {n}: 已清空日志 {lp}")
            done += 1
        except FileNotFoundError:
            print("  未找到 docker 命令，跳过。")
            return
        except Exception as exc:  # noqa: BLE001
            print(f"  {n}: 失败 {exc}")
    print(f"  完成 {done} 个容器。" if done else "  没有容器被处理。")


def act_init() -> None:
    print("  初始化 / 修复：")
    for d in (ROOT / "downloads", cache_dir(load_cfg())):
        try:
            d.mkdir(parents=True, exist_ok=True)
            print(f"   目录 OK：{d.name}/")
        except Exception as exc:  # noqa: BLE001
            print(f"   目录创建失败 {d}: {exc}")

    cfg = load_cfg()
    p = mem_path(cfg)
    if not p.exists():
        p.write_text("{}", encoding="utf-8")
        print(f"   已创建空记忆文件：{p.name}")
    else:
        s, m, md = memory_stats(p)
        if s < 0:
            _backup(p)
            p.write_text("{}", encoding="utf-8")
            print(f"   记忆文件损坏，已备份并重置：{p.name}")
        else:
            print(f"   记忆文件 OK：{s} 个会话 / {m} 条消息 / {md} 条媒体索引")

    try:
        load_cfg()
        print("   config.json 解析 OK")
    except SystemExit:
        return
    print("   提示：NapCat 登录态在 napcat/qq 与 napcat/config，"
          "本工具不会碰——清了要重新扫码。")


# ------------------------------------------------------------------ 交互
def _confirm(msg: str, auto_yes: bool = False) -> bool:
    if auto_yes:
        return True
    try:
        return input(f"{msg} [y/N]: ").strip().lower() == "y"
    except (EOFError, KeyboardInterrupt):
        return False


def _backup(p: Path) -> None:
    try:
        dst = p.with_name(f"{p.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copyfile(p, dst)
        print(f"   已备份 → {dst.name}")
    except Exception as exc:  # noqa: BLE001
        print(f"   备份失败（继续）：{exc}")


def menu(cfg: dict) -> list[int]:
    print()
    print("=" * 56)
    print("  选择要执行的项（可多选）")
    print("=" * 56)
    for no, name, desc in ITEMS:
        print(f"   {no}. {name:<22} {desc}")
    print()
    raw = input("  输入编号（空格或逗号分隔，如 1 3 7；回车取消）: ").strip()
    picks = []
    for tok in raw.replace(",", " ").split():
        if tok.isdigit() and 1 <= int(tok) <= len(ITEMS):
            picks.append(int(tok))
    return sorted(set(picks))


def run_pick(no: int, cfg: dict, auto_yes: bool) -> None:
    print(f"\n--- [{no}] {ITEMS[no - 1][1]} ---")
    if no == 1:
        act_memory(cfg, auto_yes)
    elif no == 2:
        act_downloads(auto_yes)
    elif no == 3:
        act_cache(cfg, auto_yes)
    elif no == 4:
        act_tmp(auto_yes)
    elif no == 5:
        act_remote(auto_yes)
    elif no == 6:
        act_logs(auto_yes)
    elif no == 7:
        act_init()


def main() -> None:
    argv = sys.argv[1:]
    auto_yes = "-y" in argv or "--yes" in argv
    cfg = load_cfg()

    if "--init" in argv:
        act_init()
        return

    show_stats(cfg)

    picks = [int(a) for a in argv if a.isdigit() and 1 <= int(a) <= len(ITEMS)]
    if not picks:
        if "--stats" in argv:
            return
        picks = menu(cfg)
        if not picks:
            print("\n未选择任何项，退出。")
            return

    print(f"\n将执行：{', '.join(str(p) for p in picks)}")
    if not _confirm("确认开始", auto_yes):
        print("已取消。")
        return

    for no in picks:
        run_pick(no, cfg, auto_yes)

    print()
    print("=" * 56)
    print("  完成。当前占用：")
    print("=" * 56)
    show_stats(cfg)


if __name__ == "__main__":
    main()
