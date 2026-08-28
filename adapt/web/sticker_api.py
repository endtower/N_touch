# -*- coding: utf-8 -*-
"""
adapt.web.sticker_api
=====================

LLM 自存表情包管理接口（memory/llm_save/stickers/）。

端点:
  GET    /stickers/list            — 列出全部表情包（含元数据 + 可访问 URL）
  PATCH  /stickers/{filename}      — 修改某个表情包的标题/描述/情绪（标题变更时同步重命名文件）
  DELETE /stickers/{filename}      — 删除某个表情包（图片 + 同名 .json）

只针对 LLM 自存表情包目录（memory/llm_save/stickers/），与前端 InputArea 的表情包
管理菜单对齐。命中关键失败（路径越界、文件缺失）直接抛异常阻断，不用 try-ex 吞错蔓延。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict

from aiohttp import web

from utils.logger import get_logger

logger = get_logger(__name__, "DEBUG")

# 当前文件位于: N_Touch/adapt/web/sticker_api.py
# parents[0]=web, [1]=adapt, [2]=N_Touch
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
LLM_SAVE_ROOT = _PROJECT_ROOT / "memory" / "llm_save"
STICKER_DIR = LLM_SAVE_ROOT / "stickers"

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def _sanitize_filename(title: str, max_len: int = 50) -> str:
    """把标题清洗成可作为文件名的字符串（与 VisionAgent._sanitize_filename 一致）。"""
    if not title:
        return "untitled"
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]', '', title)
    cleaned = cleaned.strip().rstrip('.')
    cleaned = re.sub(r'\s+', ' ', cleaned)
    if not cleaned:
        return "untitled"
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip()
    return cleaned


def _resolve_sticker_file(filename: str) -> Path:
    """把 filename 解析成 STICKER_DIR 下真实路径（防跨目录攻击），不存在则直接阻断。"""
    name = Path(filename).name  # 只取 basename，丢弃任何目录成分
    if not name:
        raise PermissionError(f"not a valid sticker filename: {filename}")
    target = (STICKER_DIR / name).resolve()
    try:
        target.relative_to(STICKER_DIR.resolve())
    except ValueError:
        raise PermissionError(f"not a sticker file: {filename}")
    return target


def _read_meta(img_path: Path) -> Dict[str, Any]:
    meta_path = img_path.with_suffix(".json")
    if meta_path.exists():
        return json.loads(meta_path.read_text(encoding="utf-8"))
    return {}


def _sticker_entry(img_path: Path) -> Dict[str, Any]:
    meta = _read_meta(img_path)
    return {
        "filename": img_path.name,
        "sticker_title": meta.get("sticker_title") or meta.get("title", ""),
        "description": meta.get("description", ""),
        "mood": meta.get("mood", ""),
        "saved_at": meta.get("saved_at", ""),
        "url": f"/llm-save/stickers/{img_path.name}",
    }


# ============================================================
# GET /stickers/list
# ============================================================

async def handle_list_stickers(_request: web.Request) -> web.Response:
    """列出 LLM 自存表情包，按 saved_at 倒序。"""
    STICKER_DIR.mkdir(parents=True, exist_ok=True)
    stickers: list[Dict[str, Any]] = []
    for img_path in STICKER_DIR.iterdir():
        if not img_path.is_file():
            continue
        if img_path.suffix.lower() not in _IMAGE_SUFFIXES:
            continue
        stickers.append(_sticker_entry(img_path))
    stickers.sort(key=lambda x: x.get("saved_at") or "", reverse=True)
    return web.json_response({"stickers": stickers})


# ============================================================
# PATCH /stickers/{filename} — 修改元数据（标题变化则重命名文件）
# ============================================================

async def handle_update_sticker(request: web.Request) -> web.Response:
    filename = request.match_info.get("filename", "")
    if not filename:
        return web.json_response({"error": "filename 必填"}, status=400)
    target = _resolve_sticker_file(filename)
    if not target.is_file():
        return web.json_response({"error": f"表情包 {filename} 不存在"}, status=404)

    body = await request.json()
    sticker_title = (body.get("sticker_title") or "").strip()
    description = (body.get("description") or "").strip()
    mood = (body.get("mood") or "").strip()
    if not sticker_title:
        return web.json_response({"error": "sticker_title 必填"}, status=400)

    old_meta = _read_meta(target)
    old_title = (old_meta.get("sticker_title") or old_meta.get("title") or "").strip()
    safe_title = _sanitize_filename(sticker_title)

    new_target = target
    new_meta_path = target.with_suffix(".json")
    if old_title != sticker_title:
        suffix = target.suffix.lower() or ".jpg"
        new_target = STICKER_DIR / f"{safe_title}{suffix}"
        new_meta_path = new_target.with_suffix(".json")
        if new_target.exists() and new_target != target:
            return web.json_response(
                {"error": f"目标文件已存在 {new_target.name}，请换一个新标题"},
                status=409,
            )
        target.rename(new_target)
        if new_meta_path.exists():
            # 同名 .json 若已有（不该发生）则直接阻断，避免覆盖
            raise FileExistsError(f"sticker meta already exists: {new_meta_path}")
        old_meta_path = target.with_suffix(".json")
        # target 已被 rename，但旧 meta 仍在原位置，改名到新位置
        if old_meta_path.exists():
            old_meta_path.rename(new_meta_path)

    new_meta = {
        "sticker_title": sticker_title,
        "description": description,
        "mood": mood,
        "is_sticker": True,
        "original_path": old_meta.get("original_path", ""),
        "saved_image": str(new_target),
        "saved_at": _now_str(),
    }
    with open(new_meta_path, "w", encoding="utf-8") as f:
        json.dump(new_meta, f, ensure_ascii=False, indent=2)
    logger.info(
        f"[StickerAPI] 已调整表情包: {filename} → {new_target.name}（标题: {old_title} → {sticker_title}）"
    )

    return web.json_response({"sticker": _sticker_entry(new_target)})


# ============================================================
# DELETE /stickers/{filename}
# ============================================================

async def handle_delete_sticker(request: web.Request) -> web.Response:
    filename = request.match_info.get("filename", "")
    if not filename:
        return web.json_response({"error": "filename 必填"}, status=400)
    target = _resolve_sticker_file(filename)
    if not target.is_file():
        return web.json_response({"error": f"表情包 {filename} 不存在"}, status=404)

    target.unlink()
    meta_path = target.with_suffix(".json")
    if meta_path.exists():
        meta_path.unlink()
    logger.info(f"[StickerAPI] 已删除表情包: {filename}")
    return web.json_response({"deleted": filename})


def _now_str() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
