# -*- coding: utf-8 -*-
"""通用工具：ToolHC（hc 相关工具）

灵光一现记事本（shortlight）的通用工具实现，继承 ToolHandlers：
- list_notes：按 scope 列出笔记——普通笔记（已落库事件链文档，仅给章节标题与
  条目数）/ 见闻录 / 任务相关笔记（后两者为特殊储存槽位，逐条给出条目全文，
  此处即全量，不再经 query_note）
- query_note：按章节标题查询一篇已落库普通笔记的完整内容（特殊槽位不经此工具）
- record_note：将素材记录进当前事件链的缓存文档（session 信息由调用方注入，
  LLM 无需感知；无活跃链文档属状态异常，直接上抛崩溃阻断）

schema 统一管理：list_notes / query_note 通用 schema，以及 record_to_notepad
的唯一统一 schema（按 category 分类记录，不做经验提炼）——
conversation_tool_manager / pfca_tools / social_chat_tool_manager 的 get_tools 引用，
登记逻辑仍在各角色（source 由各角色注入：conversation / social_chat / pfca）。

数据层为 brain/hc/shortlight/notepad.py 的 ShortlightNotepad 单例。
"""

from typing import Any, Dict, List

from brain.base.common_ways.tool_handlers import ToolHandlers
from brain.hc.shortlight.notepad import (
    NOTE_CATEGORIES,
    NOTE_CATEGORY_TASK,
    NOTE_CATEGORY_SELF_FIX,
    NOTE_CATEGORY_QUOTE,
    SCOPES,
    SCOPE_NORMAL,
    SLOT_EVENT,
    SLOT_NAMES,
    SLOT_TASK,
)


class ToolHC(ToolHandlers):
    """hc 通用工具：灵光一现记事本（shortlight）。"""

    # ============ 工具 schema（提示词） ============

    @classmethod
    def get_list_notes_tool_schema(cls) -> Dict[str, Any]:
        """返回 list_notes 工具的 schema。"""
        return {
            "type": "function",
            "function": {
                "name": "list_notes",
                "description": (
                    "列出记事本笔记概览。可指定 scope 选择范围：\n"
                    f"- {SCOPE_NORMAL}（默认）：已落库的事件链笔记文档，按标题分章，"
                    "每章只给章节标题与条目数，条目全文用 query_note 按该标题查询；\n"
                    f"- {SLOT_EVENT}：反思整理的现象与认识（长期累积，不随普通笔记归档），"
                    "逐条给出条目全文；\n"
                    f"- {SLOT_TASK}：任务进度相关的独立台账（任务 agent 收集整理用），"
                    "逐条给出条目全文。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "scope": {
                            "type": "string",
                            "description": (
                                "要列出的笔记范围，省略则默认普通笔记。"
                            ),
                            "enum": list(SCOPES),
                        }
                    },
                    "required": []
                }
            }
        }

    @classmethod
    def get_query_note_tool_schema(cls) -> Dict[str, Any]:
        """返回 query_note 工具的 schema。"""
        return {
            "type": "function",
            "function": {
                "name": "query_note",
                "description": (
                    "按章节标题查询一篇已落库普通笔记的完整内容"
                    "（时间、起因、总结、自我评价与全部条目）。"
                    "章节标题取自 list_notes 的普通笔记返回。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "笔记的章节标题。"
                        }
                    },
                    "required": ["title"]
                }
            }
        }

    @classmethod
    def get_record_note_tool_schema(cls) -> Dict[str, Any]:
        """返回 record_to_notepad 工具的统一 schema（按 category 分类记录）。

        conversation / social_chat / pfca 共用同一 schema；具体分类枚举
        与各分类语义由 description 自解释，source 由各角色登记时注入。
        """
        return {
            "type": "function",
            "function": {
                "name": "record_to_notepad",
                "description": (
                    "将值得留存的素材写入记事本。所有的笔记记录都是临时的，"
                    "将被后续其他agent进行后期维护与整理。\n"
                    "填写应当简洁，精简到1句话的长度左右，每一条上限60字。\n"
                    "优先记录劲最大、情绪最强烈的他人原话：照录或原话短引，"
                    "不改写、不总结、不评价；不考虑后果，不考虑道理和理性影响，"
                    "只挑冲击力最强、辨识度最高的说法。\n"
                    "必须从以下类别中选择之一：\n"
                    f"- {NOTE_CATEGORY_TASK}：与当前持有任务相关的信息，"
                    "这些信息将被任务agent收集并整理；\n"
                    f"- {NOTE_CATEGORY_SELF_FIX}：自己被对方指导、点评的经历，"
                    "自己错误的决策、经验经历等，将被重点关注用于系统的自我改进与调整；\n"
                    f"- {NOTE_CATEGORY_QUOTE}：有辨识度的他人原话样本，"
                    "如黑话、反讽、阴阳怪气、强烈情绪表达；照录原话或原话短引，"
                    "可附1-2个语体词（冷淡/夸张/反问），不写场景经过、不写效果评价。\n"
                    "见闻录由反思系统独立整理，不在本工具中记录；普通事件经过随事件链收尾总结留存。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "要记录的素材（纯文本，60 字以内，1 句话长度）。",
                        },
                        "category": {
                            "type": "string",
                            "description": "本条记录的类别，必须从列表中选择之一。",
                            "enum": list(NOTE_CATEGORIES),
                        },
                    },
                    "required": ["text", "category"]
                }
            }
        }

    # ============ 通用执行方法 ============

    @classmethod
    async def list_notes(cls, scope: str = SCOPE_NORMAL) -> str:
        """列出笔记概览。

        scope 为普通笔记（默认）时按标题分章列出已落库的事件链笔记文档，
        每章只给章节标题与条目数（章节标题即 query_note 的查询键）；
        为见闻录 / 任务相关笔记时列出对应特殊槽位台账，逐条给出条目全文
        与记录时间（特殊槽位不经 query_note）。
        """
        from brain.hc.shortlight import get_shortlight_notepad

        cleaned = (scope or "").strip() or SCOPE_NORMAL
        if cleaned not in SCOPES:
            raise ValueError(
                f"scope 非法（{cleaned!r}），必须从以下范围中选择之一：{'、'.join(SCOPES)}。"
            )
        if cleaned in SLOT_NAMES:
            notes = await get_shortlight_notepad().get_slot_notes(cleaned)
            if not notes:
                return f"【{cleaned}】暂无笔记。"
            lines = [f"【{cleaned}】共 {len(notes)} 条（查询键即条目文本）："]
            for idx, note in enumerate(notes, 1):
                lines.append(
                    f"{idx}. {(note.get('text') or '').strip()}"
                    f"（记录时间：{note.get('record_time', '')}）"
                )
            return "\n".join(lines)

        docs = await get_shortlight_notepad().get_all()
        if not docs:
            return "记事本暂无已落库的笔记。"

        # 按标题分章（同名合并），条目时间正序去重
        chapters: Dict[str, List[str]] = {}
        last_time: Dict[str, str] = {}
        for doc in docs:
            title = (doc.get("title") or "").strip()
            if not title:
                continue
            ftime = doc.get("finalize_time") or ""
            if ftime > last_time.get(title, ""):
                last_time[title] = ftime
            entries = chapters.setdefault(title, [])
            for note in doc.get("notes") or []:
                text = (note.get("text") or "").strip()
                if text and text not in entries:
                    entries.append(text)

        if not chapters:
            return "记事本暂无已落库的笔记。"

        lines = [f"记事本共 {len(chapters)} 章（章节标题即 query_note 的查询键）："]
        for title in sorted(chapters, key=lambda t: last_time.get(t, "")):
            lines.append(f"{title}（{len(chapters[title])} 条）")
        return "\n".join(lines)

    @classmethod
    async def query_note(cls, title: str) -> str:
        """按章节标题查询一篇已落库普通笔记的完整内容。

        章节标题取自 list_notes 的普通笔记返回。见闻录 / 任务相关笔记等
        特殊槽位台账由 list_notes 直接给全，不在本工具的查询范围内。
        """
        from brain.hc.shortlight import get_shortlight_notepad

        doc = await get_shortlight_notepad().get_by_title((title or "").strip())
        if doc is not None:
            return cls._render_document(doc)

        return (
            f"未找到标题为「{title}」的笔记。请核对标题"
            "（取自 list_notes 的章节标题）。"
        )

    @classmethod
    def _render_document(cls, doc: Dict[str, Any]) -> str:
        """渲染一篇普通笔记的完整内容。"""
        lines = [
            f"【{doc.get('title')}】",
            f"时间：{doc.get('begin_time', '')} → {doc.get('finalize_time', '')}",
        ]
        if doc.get("trigger"):
            lines.append(f"起因：{doc['trigger']}")
        if (doc.get("summary") or "").strip():
            lines.append(f"总结：{doc['summary'].strip()}")
        evaluation = doc.get("final_evaluation")
        if evaluation:
            lines.append(f"自我评价：{evaluation}")
        notes = doc.get("notes") or []
        if notes:
            lines.append(f"记录（{len(notes)} 条）：")
            for idx, note in enumerate(notes, 1):
                category = (note.get("category") or "").strip()
                tag = f"【{category}】" if category else ""
                # 不展示条目级时间戳（冗余无意义），时间信息统一由上方
                # 文档级 begin_time → finalize_time 承载
                lines.append(f"{idx}. {tag}{note.get('text', '')}")
        else:
            lines.append("记录：无")
        return "\n".join(lines)

    @classmethod
    async def record_note(
        cls,
        text: str,
        category: str,
        source: str,
        session_id: str = "",
        session_name: str = "",
    ) -> str:
        """记录内容进当前事件链的缓存文档，返回 note_id。

        Args:
            text: 要记录的内容（纯文本，一句话，60 字以内）。
            category: 本条记录的类别（三类之一，见 NOTE_CATEGORIES）。
            source: 记录来源（由调用方注入，如 "conversation"）。
            session_id: 会话 ID（由调用方注入）。
            session_name: 会话名（由调用方注入）。
        """
        from brain.hc.shortlight import get_shortlight_notepad

        return await get_shortlight_notepad().append_note(
            text=text,
            category=category,
            source=source,
            session_id=session_id,
            session_name=session_name,
        )
