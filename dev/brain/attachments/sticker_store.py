# -*- coding: utf-8 -*-
"""本地表情包归档，保留图片原始字节和同名 JSON 元数据。"""

import filecmp
import json
import shutil
from datetime import datetime
from pathlib import Path

from paths import data_root
from brain.attachments.preflight import ALLOWED_EXTS, is_valid_image_by_magic
from brain.attachments.resolver import normalize_filename_component


STICKER_DIR = data_root() / "memory" / "llm_save" / "stickers"


def archive_sticker(source: Path, title: str, description: str, mood: str) -> tuple[Path, bool]:
    """保存图片和元数据。相同图片重复保存时返回已有文件，避免覆盖库中条目。"""
    suffix = source.suffix.lower()
    if suffix not in ALLOWED_EXTS or not is_valid_image_by_magic(source):
        raise ValueError("源文件不是可用的表情包图片。")

    STICKER_DIR.mkdir(parents=True, exist_ok=True)
    destination = STICKER_DIR / f"{normalize_filename_component(title)}{suffix}"
    metadata = destination.with_suffix(".json")
    if metadata.is_file() and destination.is_file() and filecmp.cmp(source, destination, shallow=False):
        return destination, False

    record = {
        "sticker_title": title,
        "description": description,
        "mood": mood,
        "is_sticker": True,
        "original_path": str(source),
        "saved_image": str(destination),
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    image_created = False
    metadata_created = False
    try:
        with source.open("rb") as original, destination.open("xb") as image_file:
            image_created = True
            shutil.copyfileobj(original, image_file)
        with metadata.open("x", encoding="utf-8") as meta_file:
            metadata_created = True
            json.dump(record, meta_file, ensure_ascii=False, indent=2)
    except Exception as exc:
        if metadata_created:
            metadata.unlink()
        if image_created:
            destination.unlink()
        if isinstance(exc, FileExistsError):
            raise ValueError(f"表情包库已有同名条目，请换一个标题：{destination.stem}") from exc
        raise
    return destination, True
