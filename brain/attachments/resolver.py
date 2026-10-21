# -*- coding: utf-8 -*-
"""根据 LLM 给的文件名在媒体目录里反查真实绝对路径。

设计目标：
- 两套媒体根：
  - 系统侧（NapCat 下载）：<media_root>/{images,stickers}/<YYYY-MM-DD>/<file>.<ext>     含日期嵌套
  - LLM 侧（save_image / save_sticker 归档）：<llm_save_root>/{images,stickers}/<file>.<ext>  平铺
- 强制白名单：只允许列出的子目录
- 防路径穿越：filename 里不能含 / \\ .
- 防 symlink 逃逸：realpath() 后再白名单比对
- 防 LLM 攻击：filename 必须符合 hash/GUID/中文标题格式

Raises:
    ValueError: filename 格式非法
    FileNotFoundError: 文件不存在
    PermissionError: 路径在白名单外
"""

from paths import data_root
import os
import re
import unicodedata
from pathlib import Path
from typing import Optional

# ===================== 系统侧白名单 =====================
# - images / stickers：用户消息原图 / 表情包，由 media_downloader 落盘，hash 命名，含日期子目录
# - vision_results：recognize_image 复制的识图归档，中文标题命名（允许中文文件名）
SYSTEM_ALLOWED_SUBDIRS = ("images", "stickers", "vision_results")

# ===================== LLM 侧白名单 =====================
# - images / stickers：LLM 调用 save_image / save_sticker 主动归档的产物，平铺在对应子目录下
LLM_SAVE_ALLOWED_SUBDIRS = ("images", "stickers")

# ===================== 文件名字符白名单（保存侧 / 发送侧同源） =====================
# 单一事实来源：保存侧 normalize_filename_component() 与发送侧 FILENAME_PATTERN
# 都从下面的常量派生，保证「清洗后能落盘的名字」一定「发送时能通过白名单」。
#
# 允许的字符集合：
#   1. 英文字母 / 数字
#   2. 下划线 / 连字符 / 点 / 花括号（GUID 形如 {XXXX-....}）
#   3. 半角「基础符号」：Windows 合法 且 非空格的常见标点
#   4. 中日韩统一表意文字（\u4e00-\u9fff）
#
# 明确排除（Windows 文件名非法字符 + glob 通配符，禁止放行）：
#   \ / : * ? " < > |   以及空格
_ALLOWED_ASCII_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "_-.{}"                # 基础：下划线 / 连字符 / 点 / 花括号（GUID）
    "!,;'()~@#$%^&+="      # 半角基础符号（安全集：Windows 合法 且 非 glob 通配符、非空格）
)

# 文件名格式：hash / GUID / 带前缀 hash / 中文标题（vision_results / llm_save 用）
# 真实例子：
#   30FBB5FBD24B8D4B2660C3E3C0515F76.gif
#   {6244732F-E756-86EA-3FAC-9D1C566DFB0E}.png
#   ae-aed7e544de64ed1ec832e3af6587a132.gif
#   02-0299cab98ab51c2efb02c09156b9a5a6.jpg
#   橘白相间猫咪面部特写.jpg
#   模糊的动物面部特写.png
#   算了,我还是摆了.jpg   ← 全角标点已在保存侧折成半角
_FILENAME_CHARS_CLASS = "".join(
    sorted(re.escape(ch) for ch in _ALLOWED_ASCII_CHARS)
)
FILENAME_PATTERN = re.compile(
    rf"^[{_FILENAME_CHARS_CLASS}\u4e00-\u9fff]{{1,128}}\.[a-zA-Z0-9]{{1,5}}$"
)

# ===================== 中文标点 → 半角 归一化表 =====================
# NFKC 已能把「兼容形式」的全角字符折半角（如 ，！；：（） ），此处补齐
# NFKC 不会处理的汉字系标点（。、·…“”「」《》等）。
_CJK_PUNCT_MAP = {
    "。": ".", "、": ",", "·": ".", "・": ".", "…": ".", "‥": ".",
    "—": "-", "–": "-", "―": "-", "ー": "-",
    "～": "~", "〜": "~",
    "「": '"', "」": '"', "『": '"', "』": '"', "“": '"', "”": '"',
    "‘": "'", "’": "'",
    "《": "<", "》": ">", "〈": "<", "〉": ">",
    "【": "[", "】": "]", "〔": "(", "〕": ")",
}


def normalize_filename_component(text: str, max_len: int = 50) -> str:
    """把任意标题/文本归一化为「可安全落盘 且 能通过 FILENAME_PATTERN」的文件名主干。

    保存侧（VisionAgent / sticker_api）与发送侧（FILENAME_PATTERN）同源：
    本函数的输出字符集严格 ⊆ FILENAME_PATTERN 允许集，因此「存得下」必然「发得出」。

    规则：
    1. NFKC 兼容折叠：全角字母 / 数字 / 标点 → 半角（，！；：（） 等）
    2. 汉字系标点补齐折叠（。、·…“”「」《》，NFKC 不处理的部分）
    3. 白名单外字符（空格、Windows 非法字符、glob 通配符等）→ 下划线 _
    4. 去首尾点 / 下划线，长度截断；清洗后为空 → "untitled"

    Args:
        text: 原始标题
        max_len: 主干最大长度（不含扩展名）

    Returns:
        归一化后的文件名主干（不含扩展名）
    """
    if not isinstance(text, str) or not text.strip():
        return "untitled"

    # 1. NFKC 兼容折叠（全角 → 半角）
    folded = unicodedata.normalize("NFKC", text)
    # 2. 汉字系标点补齐
    folded = "".join(_CJK_PUNCT_MAP.get(ch, ch) for ch in folded)

    # 3. 白名单外一律降级为下划线
    cleaned = "".join(
        ch if (ch in _ALLOWED_ASCII_CHARS or "\u4e00" <= ch <= "\u9fff") else "_"
        for ch in folded
    )

    # 4. 去首尾点 / 下划线（首尾点会导致隐藏文件 + resolve 拒绝）
    cleaned = cleaned.strip("._")

    if not cleaned:
        return "untitled"

    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].strip("._")

    return cleaned or "untitled"


def _get_media_root() -> Path:
    """从 adapt_settings 读 MEDIA_BASE_DIR，并解析为绝对路径。

    MEDIA_BASE_DIR 是相对项目根的路径（"memory/chat_history/media"），
    解析为相对于当前工作目录的绝对路径。
    """
    from config.adapt_settings import get_onebot_config
    cfg = get_onebot_config()
    return Path(cfg.MEDIA_BASE_DIR).resolve()


def _get_llm_save_root() -> Path:
    """LLM 自存媒体根目录：<项目根>/memory/llm_save"""
    # 直接基于项目根拼绝对路径，避免依赖 cwd
    return data_root() / "memory" / "llm_save"


def _search_in_root(
    media_root: Path,
    allowed_subdirs: tuple,
    filename: str,
    *,
    nested_by_date: bool,
) -> str | None:
    """在指定媒体根下查找 filename。

    Args:
        media_root: 媒体根目录
        allowed_subdirs: 允许的子目录白名单
        filename: 纯文件名
        nested_by_date: True=按 <YYYY-MM-DD>/ 嵌套查找（系统侧）；False=平铺查找（LLM 侧）

    Returns:
        命中的绝对路径字符串；未命中返回 None。
    """
    for subdir in allowed_subdirs:
        if nested_by_date:
            # 系统侧：<root>/<subdir>/<YYYY-MM-DD>/<filename>
            # 用目录遍历 + 精确名比对，绝不把 filename 拼进 glob，
            # 避免 filename 里的通配符（* ? [）被解释成模式造成越权命中。
            subdir_path = media_root / subdir
            if not subdir_path.is_dir():
                continue
            for day_dir in sorted(subdir_path.iterdir()):
                if not day_dir.is_dir():
                    continue
                candidate = day_dir / filename
                if not candidate.is_file():
                    continue
                resolved = candidate.resolve()
                try:
                    resolved.relative_to(media_root)
                except ValueError:
                    raise PermissionError(f"path outside media whitelist: {resolved}")
                return str(resolved)
        else:
            # LLM 侧：<root>/<subdir>/<filename>  （平铺）
            candidate = media_root / subdir / filename
            if candidate.is_file():
                resolved = candidate.resolve()
                try:
                    resolved.relative_to(media_root)
                except ValueError:
                    raise PermissionError(f"path outside llm_save whitelist: {resolved}")
                return str(resolved)
    return None


def resolve_media_file(filename: str) -> str:
    """根据 filename 在两套媒体目录里查找：
    1) LLM 侧（自存归档）：memory/llm_save/{images,stickers}/   平铺
    2) 系统侧（NapCat 下载）：memory/chat_history/media/{images,stickers}/<YYYY-MM-DD>/

    优先命中 LLM 侧（自存优先）。

    设计动机：
    - LLM 侧是 LLM 主动 save_image / save_sticker 归档的素材，通常是 LLM
      明确知道"我可以用"的资源（表情包 / 自己存的梗图等），mtime 也是近期。
    - 系统侧是 NapCat 下载的历史原图，mtime 可能因备份 / 迁移被破坏成老时间，
      会被 preflight 的 30 天年龄护栏挡掉。
    - 优先 LLM 侧能避免"系统侧历史同名旧图被预检拦截、而 LLM 侧同一文件
      用不到"的死锁。

    Args:
        filename: 纯文件名（含扩展名），如 '30FBB...F76.gif'

    Returns:
        绝对路径字符串

    Raises:
        ValueError: filename 格式非法（空 / 含路径分隔符 / 格式不匹配）
        FileNotFoundError: 文件在两套媒体根下都不存在
        PermissionError: 文件存在但不在白名单子目录内（防 symlink 逃逸）
    """
    # 1. 格式校验
    if not filename or not isinstance(filename, str):
        raise ValueError(f"filename is empty or not a string: {filename!r}")
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise ValueError(f"filename contains path separator: {filename!r}")
    if not FILENAME_PATTERN.match(filename):
        raise ValueError(f"filename format not allowed: {filename!r}")

    # 2. 先查 LLM 侧（平铺）
    llm_root = _get_llm_save_root()
    hit = _search_in_root(
        llm_root,
        LLM_SAVE_ALLOWED_SUBDIRS,
        filename,
        nested_by_date=False,
    )
    if hit is not None:
        return hit

    # 3. 再查系统侧（带日期嵌套）
    system_root = _get_media_root()
    hit = _search_in_root(
        system_root,
        SYSTEM_ALLOWED_SUBDIRS,
        filename,
        nested_by_date=True,
    )
    if hit is not None:
        return hit

    raise FileNotFoundError(f"file not found in media or llm_save: {filename}")