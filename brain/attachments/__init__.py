# -*- coding: utf-8 -*-
"""附件系统：发图链路

职责：
1. resolver：根据 LLM 给的文件名在 media 目录里反查真实绝对路径（白名单 + 防 symlink）
2. preflight：发图前做基本校验（格式 / magic bytes / 尺寸 / EXIF 剥除）

设计原则：
- 复用 media_downloader 已落盘的 media/images/ 和 media/stickers/ 目录
- LLM 只需要知道文件名（content_text 里 [图片:xxx] 那个 xxx），不需要路径
- 服务端自动补全路径并校验，LLM 永远拿不到完整路径
"""

from brain.attachments.resolver import resolve_media_file
from brain.attachments.preflight import preflight

__all__ = ["resolve_media_file", "preflight"]
