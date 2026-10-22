# -*- coding: utf-8 -*-
"""表情包与图片：查看、读进上下文、保存、修改。

落盘位置沿用主仓：
  memory/llm_save/stickers/   表情包（xxx.jpg + xxx.json 同名元数据）
  memory/llm_save/images/     普通图片
元数据字段沿用主仓：sticker_title / description / mood / is_sticker / saved_at

读图不需要 Vision Agent：主模型本身是多模态的，直接把它塞进上下文就行。
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(r"E:\ABYSS\N_Touch\memory\llm_save")
STICKERS = ROOT / "stickers"
IMAGES = ROOT / "images"
INCOMING = Path(r"E:\ABYSS\N_Touch\memory\chat_history\media")
IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def _dirs(kind: str) -> List[Path]:
    if kind == "images":
        return [IMAGES, STICKERS]
    return [STICKERS, IMAGES]


def _meta_path(img: Path) -> Path:
    return img.with_suffix(".json")


def load_meta(img: Path) -> Dict[str, Any]:
    p = _meta_path(img)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def list_items(kind: str = "stickers", query: str = "", limit: int = 20) -> str:
    """列出有什么。query 命中标题/描述/情绪。"""
    q = (query or "").strip()
    rows: List[str] = []
    for d in _dirs(kind):
        if not d.exists():
            continue
        for img in sorted(d.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if img.suffix.lower() not in IMG_EXT:
                continue
            meta = load_meta(img)
            title = meta.get("sticker_title") or img.stem
            mood = meta.get("mood") or ""
            desc = meta.get("description") or ""
            blob = f"{title}{mood}{desc}{img.name}"
            if q and q not in blob:
                continue
            tag = "表情包" if d.name == "stickers" else "图片"
            rows.append(f"{img.name}｜{title}｜{mood}".replace("｜｜", "｜") + f"（{tag}）")
            if len(rows) >= limit:
                break
        if len(rows) >= limit:
            break
    if not rows:
        return f"没有{'相关的' if q else ''}表情包或图片。" if q else "库是空的。"
    head = f"共 {len(rows)} 张（用 sticker_view 可以把其中一张读进来看）"
    return head + "\n" + "\n".join(rows)


def _resolve(ref: str, kind: str = "") -> Optional[Path]:
    """文件名 / 词干 / 绝对路径 → 真实文件。只在允许的目录里找。"""
    ref = (ref or "").strip().strip('"')
    if not ref:
        return None
    raw = Path(ref)
    if raw.is_absolute():
        return raw if raw.exists() else None
    for d in _dirs(kind or "stickers"):
        cand = d / ref
        if cand.exists():
            return cand
        for ext in IMG_EXT:
            cand = d / (ref + ext)
            if cand.exists():
                return cand
        # 词干模糊匹配（她的标题常和文件名不完全一致）
        if d.exists():
            for img in d.iterdir():
                if img.suffix.lower() in IMG_EXT and (ref in img.stem or ref in str(load_meta(img).get("sticker_title", ""))):
                    return img
    return None


def read_image(ref: str, kind: str = "") -> Dict[str, Any]:
    """把一张图读进上下文。返回给 loop 的指令包（不是纯文本）。"""
    path = _resolve(ref, kind)
    if path is None:
        return {"text": f"找不到这张图：{ref}"}
    if path.stat().st_size > 6 * 1024 * 1024:
        return {"text": f"图片太大（{path.stat().st_size // 1024} KB），读不进来"}
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    b64 = base64.b64encode(path.read_bytes()).decode()
    meta = load_meta(path)
    return {
        "__image__": True,
        "name": path.name,
        "mime": mime,
        "b64": b64,
        "text": f"已把 {path.name} 读进来看（{meta.get('sticker_title', '')}）",
    }


def save_sticker(src: str, title: str, mood: str = "", description: str = "") -> str:
    """把一张图存成表情包：复制到 stickers/，写同名元数据。"""
    path = _resolve(src) or _resolve(src, "images")
    if path is None:
        # 允许从收到的媒体目录里拿
        cand = _search_incoming(src)
        if cand is None:
            return f"找不到源图：{src}"
        path = cand
    title = (title or "").strip() or path.stem
    STICKERS.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch for ch in title if ch not in '\\/:*?"<>|').strip() or "sticker"
    dest = STICKERS / f"{safe}{path.suffix.lower()}"
    shutil.copy2(path, dest)
    _write_meta(dest, title, mood, description)
    return f"已存成表情包：{dest.name}"


def adjust_sticker(ref: str, title: str = "", mood: str = "", description: str = "") -> str:
    """改一张已存在表情包的元数据（标题/情绪/描述）。文件不动。"""
    path = _resolve(ref)
    if path is None:
        return f"找不到这张表情包：{ref}"
    old = load_meta(path)
    _write_meta(
        path,
        title or old.get("sticker_title") or path.stem,
        mood or old.get("mood") or "",
        description or old.get("description") or "",
    )
    return f"已更新 {path.name} 的元数据"


def _write_meta(img: Path, title: str, mood: str, description: str) -> None:
    meta = {
        "sticker_title": title,
        "description": description,
        "mood": mood,
        "is_sticker": True,
        "saved_image": str(img),
        "saved_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _meta_path(img).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _search_incoming(ref: str) -> Optional[Path]:
    if not INCOMING.exists():
        return None
    name = Path(ref).name
    for f in INCOMING.rglob("*"):
        if f.is_file() and f.suffix.lower() in IMG_EXT and (f.name == name or ref in f.name):
            return f
    return None
