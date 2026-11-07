# -*- coding: utf-8 -*-
"""把 memory/chat_history/*.json 打包到 memory/backups/chat_history_<timestamp>.zip。

只读，**不**触碰原文件。可重复运行（每次产出独立时间戳文件）。

设计要点：
- 不递归，只匹配 *.json（避开 media/ 子目录里的图片等大文件，避免无意义膨胀）
- 压缩级别 9（聊天 JSON 重复率高，压缩比通常 10x+，磁盘友好）
- 空目录时直接退出，不报错

用法：
    python brain/tools/backup_chat_history.py
"""

import sys
import zipfile
from datetime import datetime
from pathlib import Path


CHAT_HISTORY_DIR_NAME = "chat_history"
REPO_ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    src_dir = REPO_ROOT / "memory" / CHAT_HISTORY_DIR_NAME
    if not src_dir.is_dir():
        print(f"[FAIL] 目录不存在: {src_dir}", file=sys.stderr)
        return 1

    files = sorted(p for p in src_dir.glob("*.json"))
    if not files:
        print(f"[INFO] {src_dir} 下没有 .json 文件，无需备份")
        return 0

    backups_dir = REPO_ROOT / "memory" / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = backups_dir / f"chat_history_{ts}.zip"

    # 包内保留 "chat_history/xxx.json" 这个相对路径结构，方便解压时直接还原
    arcname_prefix = CHAT_HISTORY_DIR_NAME + "/"
    total_size = 0
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for p in files:
            arcname = arcname_prefix + p.name
            zf.write(p, arcname)
            total_size += p.stat().st_size

    zipped_size = out_path.stat().st_size
    print(f"[OK] 备份完成：{out_path}")
    print(f"     文件数：{len(files)}")
    print(f"     原大小：{_fmt(total_size)}")
    print(f"     压缩后：{_fmt(zipped_size)} "
          f"(压缩比 {zipped_size / total_size:.1%})" if total_size else "")
    return 0


def _fmt(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.2f} MB"


if __name__ == "__main__":
    sys.exit(main())
