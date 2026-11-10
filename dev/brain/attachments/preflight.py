# -*- coding: utf-8 -*-
"""发图前基本校验（不包含 NSFW / 政治敏感内容审核，按用户场景只做格式/尺寸/EXIF）。

校验项：
1. 扩展名白名单
2. magic bytes 校验（防伪扩展名）
3. 尺寸 / 大小限制（自动 PIL 压缩）
4. 文件年龄（30 天前不发；LLM 侧自存素材豁免，详见 _is_llm_saved_media）
5. EXIF 剥除（隐私保护）
"""

from paths import data_root
import os
import time
from pathlib import Path
from typing import Dict

# 允许的扩展名
ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

# 限制
MAX_SIZE_BYTES = 5 * 1024 * 1024       # 5MB
MAX_DIMENSION = 4096                    # 长边最大 4096px
MAX_AGE_DAYS = 30                       # 30 天前的图不发（系统侧下载适用）


def _is_llm_saved_media(path: str) -> bool:
    """判定 path 是否位于 LLM 侧自存媒体根（memory/llm_save/{images,stickers}/）。

    归档素材可长期使用。旧归档可能保留源文件的 mtime，也按根路径豁免。

    与 brain.attachments.resolver._get_llm_save_root() 的项目根算法保持一致：
        <项目根>/memory/llm_save
    """
    try:
        llm_root = (data_root() / "memory" / "llm_save").resolve()
        target = Path(path).resolve()
        target.relative_to(llm_root)
        return True
    except (ValueError, OSError):
        return False


def is_valid_image_by_magic(path: str | Path) -> bool:
    """通过 magic bytes 验证文件真是图片（不是改名后的 exe 等）。"""
    try:
        with open(path, "rb") as f:
            header = f.read(16)
    except Exception:
        return False
    if header.startswith(b"\x89PNG\r\n\x1a\n"):  return True   # PNG
    if header.startswith(b"\xff\xd8\xff"):        return True   # JPEG
    if header[:6] in (b"GIF87a", b"GIF89a"):      return True   # GIF
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":  return True  # WebP
    if header[:2] == b"BM":                       return True   # BMP
    return False


def _resize_if_oversize(path: str) -> None:
    """超尺寸 / 超大图，自动等比缩到 4096px 内 + 质量压缩。"""
    try:
        from PIL import Image
    except ImportError:
        return  # PIL 不可用就跳过

    img = Image.open(path)
    w, h = img.size
    need_resize = max(w, h) > MAX_DIMENSION or os.path.getsize(path) > MAX_SIZE_BYTES
    if not need_resize:
        img.close()
        return

    ratio = MAX_DIMENSION / max(w, h)
    new_size = (int(w * ratio), int(h * ratio))
    img = img.resize(new_size, Image.LANCZOS)

    # 保留原格式（BMP 转 PNG 因为 Pillow 不支持 BMP 写入有损压缩）
    save_format = img.format
    if save_format == "BMP":
        save_format = "PNG"
    save_kwargs = {"format": save_format, "optimize": True}
    if save_format in ("JPEG", "WEBP"):
        save_kwargs["quality"] = 85
    img.save(path, **save_kwargs)


def _strip_exif(path: str) -> None:
    """剥 EXIF（隐私保护：GPS / 设备型号 / 缩略图等）。"""
    try:
        from PIL import Image
    except ImportError:
        return

    img = Image.open(path)
    if img.info.get("exif") is None:
        img.close()
        return
    data = list(img.getdata())
    clean = Image.new(img.mode, img.size)
    clean.putdata(data)
    save_format = img.format or "PNG"
    save_kwargs = {"format": save_format, "optimize": True}
    if save_format in ("JPEG", "WEBP"):
        save_kwargs["quality"] = 85
    clean.save(path, **save_kwargs)


def preflight(path: str) -> Dict:
    """发图前检查 + 修正。返回 {"ok": bool, "reason": str, "path": str}。

    失败时不会抛异常，方便上层返回错误消息给 LLM。
    """
    # 1. 扩展名
    ext = os.path.splitext(path)[1].lower()
    if ext not in ALLOWED_EXTS:
        return {"ok": False, "reason": f"unsupported format: {ext}", "path": path}

    # 2. magic bytes（防伪扩展名）
    if not is_valid_image_by_magic(path):
        return {"ok": False, "reason": "file is not a valid image (magic bytes mismatch)", "path": path}

    # 3. 年龄（仅对系统侧下载适用；LLM 侧自存素材的 mtime 不代表保存时刻，全豁免）
    if not _is_llm_saved_media(path):
        if time.time() - os.path.getmtime(path) > MAX_AGE_DAYS * 86400:
            return {
                "ok": False,
                "reason": (
                    f"当前引用的是超过 {MAX_AGE_DAYS} 天的聊天缓存副本，无法直接发送。"
                    "如果要长期使用且文件仍在，可先保存到表情包库，再用保存工具返回的文件名发送。"
                ),
                "path": path,
            }

    # 4. 大小 / 尺寸（自动压缩）
    try:
        _resize_if_oversize(path)
    except Exception as e:
        return {"ok": False, "reason": f"resize failed: {e}", "path": path}

    # 5. 剥 EXIF（仅对有 EXIF 的格式有效，GIF/BMP 跳过）
    if ext in {".jpg", ".jpeg", ".png", ".webp"}:
        try:
            _strip_exif(path)
        except Exception:
            pass  # EXIF 剥除失败不阻塞发图

    return {"ok": True, "path": path, "size_bytes": os.path.getsize(path)}
