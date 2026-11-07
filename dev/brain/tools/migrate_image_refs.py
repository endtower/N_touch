# -*- coding: utf-8 -*-
"""
brain.tools.migrate_image_refs
==============================

把 chat_history/*.json 里所有 content_text 中的 [图片] / [图片:xxx] 等占位符
替换为 [图片:filename.ext] 格式, 让纯文本里也能看到具体文件名。

支持的类型（content segment type -> 渲染前缀）:
    image -> [图片:filename.ext]
    video -> [视频:filename.ext]
    audio -> [语音]  (不暴露文件名, 怕太长)
    file  -> [文件:filename.ext]
    json  -> [富文本]
    text  -> 直接用 data.text
    其余  -> [类型]

策略：
- 同一段如果只有 image / video / file 等媒体类型，把占位符的 [图片] 替换成 [图片:xxx.ext]
- 文本和图片混排：保留文本，图片部分单独替换
- 已经包含文件名（[图片:xxx]、[视频:xxx]）的不动
- 自动备份原文件到 .bak（首次运行）

用法：
    python brain/tools/migrate_image_refs.py                # 处理整个 chat_history/
    python brain/tools/migrate_image_refs.py --no-backup    # 不备份
    python brain/tools/migrate_image_refs.py --dry-run      # 只打印不写
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple


# 仓库根 = brain/tools/migrate_image_refs.py -> parents[2]
CHAT_HISTORY_DIR = Path(__file__).resolve().parents[2] / "memory" / "chat_history"

# 占位符正则: [图片] / [图片:已有文件名] / [视频:xxx] / [文件:xxx]
_RE_IMAGE = re.compile(r"\[图片(?::([^\]]+))?\]")
_RE_VIDEO = re.compile(r"\[视频(?::([^\]]+))?\]")
_RE_FILE  = re.compile(r"\[文件(?::([^\]]+))?\]")


def _normalize_filename(f: str) -> str:
    """如果 file 是绝对路径或带目录, 只保留 basename (兼容 qq 和 web 两种存储格式)。"""
    if not f:
        return ""
    # 处理 Windows / POSIX 路径分隔符
    f = f.replace("\\", "/")
    if "/" in f:
        f = f.rsplit("/", 1)[-1]
    return f


def _segment_label(seg: Dict) -> str:
    """一段内容 -> 占位符字符串。"""
    st = seg.get("type")
    sd = seg.get("data") or {}
    if st == "text":
        return sd.get("text", "")
    if st == "image":
        f = _normalize_filename(sd.get("file") or sd.get("filename") or "")
        return f"[图片:{f}]" if f else "[图片]"
    if st == "video":
        f = _normalize_filename(sd.get("file") or "")
        return f"[视频:{f}]" if f else "[视频]"
    if st == "audio":
        return "[语音]"
    if st == "file":
        f = _normalize_filename(sd.get("file") or "")
        return f"[文件:{f}]" if f else "[文件]"
    if st == "json":
        return "[富文本]"
    if st == "at":
        qq = sd.get("qq", "")
        name = sd.get("name", "")
        return f"[@{name or qq}]"
    if st == "reply":
        return "[回复]"
    return f"[{st}]"


def rebuild_content_text(content: List[Dict]) -> str:
    """把 segments 重组成 content_text。"""
    parts = [_segment_label(seg) for seg in content if isinstance(seg, dict)]
    return "".join(parts)


def migrate_message(msg: Dict) -> Tuple[bool, str]:
    """处理单条消息, 返回 (是否变更, 新 content_text)."""
    content = msg.get("content")
    if not isinstance(content, list):
        return False, msg.get("content_text", "")

    new_text = rebuild_content_text(content)
    old_text = msg.get("content_text", "")
    if new_text == old_text:
        return False, old_text
    return True, new_text


def migrate_file(path: Path, *, dry_run: bool = False) -> Tuple[int, int]:
    """处理一个 chat_history 文件, 返回 (变动消息数, 总消息数)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"  [SKIP] 读取失败 {path.name}: {e}")
        return 0, 0

    msgs = raw.get("message_list") or []
    changed = 0
    new_msgs = []
    for m in msgs:
        ok, new_text = migrate_message(m)
        if ok:
            m = {**m, "content_text": new_text}
            changed += 1
        new_msgs.append(m)
    raw["message_list"] = new_msgs

    if changed == 0:
        return 0, len(msgs)

    if dry_run:
        print(f"  [DRY] {path.name}: {changed}/{len(msgs)} 条会变更")
        # 打印前 3 条变更样例
        shown = 0
        for m in new_msgs:
            if shown >= 3:
                break
            old_text = (m.get("content_text") or "")[:80]
            # 原始 content_text 已经被覆盖了, 跳过
        return changed, len(msgs)

    # 备份原文件
    bak = path.with_suffix(path.suffix + ".bak")
    if not bak.exists():
        bak.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"  [BAK] 备份到 {bak.name}")

    path.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"  [OK]  {path.name}: {changed}/{len(msgs)} 条已更新")
    return changed, len(msgs)


def main() -> int:
    parser = argparse.ArgumentParser(description="迁移 chat_history 图片引用格式")
    parser.add_argument("--no-backup", action="store_true", help="不创建 .bak 备份")
    parser.add_argument("--dry-run", action="store_true", help="只打印不写")
    parser.add_argument("--dir", type=Path, default=CHAT_HISTORY_DIR, help="chat_history 目录")
    args = parser.parse_args()

    chat_dir: Path = args.dir
    if not chat_dir.is_dir():
        print(f"[ERROR] 目录不存在: {chat_dir}", file=sys.stderr)
        return 2

    files = sorted(p for p in chat_dir.glob("*.json"))
    if not files:
        print(f"[INFO] {chat_dir} 下没有 .json 文件")
        return 0

    print(f"[INFO] 处理目录: {chat_dir}")
    print(f"[INFO] 文件数: {len(files)}, dry_run={args.dry_run}")
    total_changed = 0
    total_msgs = 0
    for p in files:
        c, n = migrate_file(p, dry_run=args.dry_run)
        total_changed += c
        total_msgs += n
    print(f"\n[DONE] 共 {total_changed}/{total_msgs} 条消息内容变更, 涉及 {len(files)} 个文件")
    if total_changed > 0 and not args.dry_run:
        print("       原文件已备份为 .bak, 如确认无问题可以手动删除")
    return 0


if __name__ == "__main__":
    sys.exit(main())
