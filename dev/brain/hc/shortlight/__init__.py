"""shortlight —— 灵光一现记事本。

存放 conversation 交互过程中"值得留存的素材"（纠正、教训、注意事项等），
以纯文本 + 系统自动元数据的形式持久化到 notes/ 子目录，供反思系统后续消费。

注意：本模块新增对外接口后，必须同步更新 docs/api/hc.md（scripts/check_api_docs.py 会校验）。
"""

from .notepad import (
    ShortlightNotepad,
    get_shortlight_notepad,
    NOTE_CATEGORIES,
    NOTE_CATEGORY_TASK,
    NOTE_CATEGORY_EVENT,
    NOTE_CATEGORY_SELF_FIX,
    NOTE_CATEGORY_QUOTE,
    SLOT_EVENT,
    SLOT_TASK,
    SLOT_NAMES,
    CATEGORY_TO_SLOT,
    SCOPE_NORMAL,
    SCOPES,
)

__all__ = [
    "ShortlightNotepad",
    "get_shortlight_notepad",
    "NOTE_CATEGORIES",
    "NOTE_CATEGORY_TASK",
    "NOTE_CATEGORY_EVENT",
    "NOTE_CATEGORY_SELF_FIX",
    "NOTE_CATEGORY_QUOTE",
    "SLOT_EVENT",
    "SLOT_TASK",
    "SLOT_NAMES",
    "CATEGORY_TO_SLOT",
    "SCOPE_NORMAL",
    "SCOPES",
]
