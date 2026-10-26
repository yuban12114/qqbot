#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本次升级涉及的 config.json 字段同步到服务器（幂等）。

只改下面这些字段，不动 ws_url / token / api_key 等与环境相关的项：

    openai.max_tokens             -> 4000
    reply_rules.max_reply_length  -> 4000
    timeouts.*                    -> 新增段（含 recover_reply）

为什么不用 scp 覆盖 config.json：服务器上那份是生产配置，
本地这份是开发副本，直接覆盖会把 ws_url、QQ token、API key 一起换掉。

用法：
    python3 patch_config.py                    # 默认 /opt/qqbot/config.json
    python3 patch_config.py /path/to/config.json

没有任何字段需要改时不做任何写入（幂等）；
有改动时先备份成 config.json.bak.<时间戳> 再原子替换。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

DEFAULT_PATH = "/opt/qqbot/config.json"

# 扁平字段：段名 -> 键 -> 期望值
FLAT = {
    ("openai", "max_tokens"): 4000,
    ("reply_rules", "max_reply_length"): 4000,
}

# timeouts 段：本次新增，键顺序也按这个来
TIMEOUTS = {
    "onebot_api": 10,
    "recover_reply": 60,
    "ffmpeg": 120,
    "file_upload": 240,
    "video_understand": 240,
    "event_handle": 1200,
}


def main() -> int:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PATH)

    if not target.is_file():
        print(f"[X] 找不到配置文件：{target}")
        return 1

    try:
        cfg = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 只需要报错，不关心异常类型
        print(f"[X] 配置不是合法 JSON，未做任何修改：{exc}")
        return 1

    if not isinstance(cfg, dict):
        print(f"[X] 配置根节点不是对象（{type(cfg).__name__}），未做任何修改")
        return 1

    changes: list[str] = []

    for (section, key), want in FLAT.items():
        node = cfg.get(section)
        if not isinstance(node, dict):
            node = {}
            cfg[section] = node
        old = node.get(key, "<缺失>")
        if old != want:
            node[key] = want
            changes.append(f"{section}.{key}: {old} -> {want}")

    section = cfg.get("timeouts")
    if not isinstance(section, dict):
        section = {}
        cfg["timeouts"] = section
    for key, want in TIMEOUTS.items():
        old = section.get(key, "<缺失>")
        if old != want:
            section[key] = want
            changes.append(f"timeouts.{key}: {old} -> {want}")

    if not changes:
        print("[=] 相关字段已是最新，未写入文件")
        return 0

    backup = Path(f"{target}.bak.{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(target, backup)

    tmp = Path(f"{target}.tmp")
    tmp.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, target)

    print(f"[+] 原配置已备份：{backup}")
    for line in changes:
        print(f"    {line}")
    print("[+] 已写回（其余字段保持不变）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
