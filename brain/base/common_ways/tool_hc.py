# -*- coding: utf-8 -*-
"""通用工具：ToolHC（hc 相关工具）

灵光一现记事本（shortlight）的通用工具实现，继承 ToolHandlers：
- list_notes：列出所有已落库笔记（每事件链一份文档）
- query_note：按标题查询一篇笔记的完整内容
- record_note：将素材记录进当前事件链的缓存文档（session 信息由调用方注入，
  LLM 无需感知；无活跃链文档属状态异常，直接上抛崩溃阻断）

schema 统一管理：list_notes / query_note 通用 schema，以及 record_to_notepad
的两版场景 schema（均定位为观察采集对方素材，不做经验提炼）——
conversation_tool_manager / pfca_tools / social_chat_tool_manager 的 get_tools 引用
（conversation 版由 conversation 与 social_chat 共用），登记逻辑仍在各角色
（source 由各角色注入：conversation / social_chat / pfca）。

数据层为 brain/hc/shortlight/notepad.py 的 ShortlightNotepad 单例。
"""

from typing import Any, Dict, List

from brain.base.common_ways.tool_handlers import ToolHandlers


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
                    "列出记事本中所有已落库的笔记文档"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
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
                    "按标题查询一篇笔记的完整内容"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "笔记标题。"
                        }
                    },
                    "required": ["title"]
                }
            }
        }

    @classmethod
    def get_conversation_record_note_tool_schema(cls) -> Dict[str, Any]:
        """返回 record_to_notepad 工具的 schema（conversation 版：对话现场观察素材）。"""
        return {
            "type": "function",
            "function": {
                "name": "record_to_notepad",
                "description": (
                    "将对话中观察到的对方素材写入记事本缓存，供反思系统后续提炼。"
                    "优先记录对方的言论（原话短引最佳）、要求与标准、好恶、反应模式；"
                    "完整事件经过不用记（由 terminate 的 summary 承担）。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "观察素材（纯文本40字以内），以对方为主体。",
                        },
                        "usage": {
                            "type": "string",
                            "description": "可选。补充语境：场合、来由、限定条件等",
                        },
                    },
                    "required": ["text"]
                }
            }
        }

    @classmethod
    def get_pfca_record_note_tool_schema(cls) -> Dict[str, Any]:
        """返回 record_to_notepad 工具的 schema（pfca 版：策略过程观察素材）。"""
        return {
            "type": "function",
            "function": {
                "name": "record_to_notepad",
                "description": (
                    "将策略/决策过程中观察到的对方素材写入记事本缓存，供反思系统后续提炼。"
                    "优先记录对方的言论（原话短引最佳）、要求与标准、好恶、反应模式；"
                    "事件经过与完整叙事不用记（由 terminate 的 summary 承担）。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "观察素材（纯文本，40 字以内），以对方为主体。",
                        },
                        "usage": {
                            "type": "string",
                            "description": (
                                "可选。补充语境：场合、来由、限定条件等"
                                "（不限长，query_note 查详情时可见）。"
                            ),
                        },
                    },
                    "required": ["text"]
                }
            }
        }

    # ============ 通用执行方法 ============

    @classmethod
    async def list_notes(cls) -> str:
        """列出所有已落库笔记概览（章节式：标题 + └ 条目，与 render_manual 同构
        但不限幅不截断；为 query_note 提供标题来源，元数据细节走 query_note）。"""
        from brain.hc.shortlight import get_shortlight_notepad

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

        lines = [f"记事本共 {len(chapters)} 章："]
        for title in sorted(chapters, key=lambda t: last_time.get(t, "")):
            lines.append(title)
            lines.extend(f"└ {t}" for t in chapters[title])
        return "\n".join(lines)

    @classmethod
    async def query_note(cls, title: str) -> str:
        """按标题查询一篇笔记的完整内容。"""
        from brain.hc.shortlight import get_shortlight_notepad

        doc = await get_shortlight_notepad().get_by_title(title)
        if doc is None:
            return f"未找到标题为「{title}」的笔记。请核对标题，标题应来自记事本手册的笔记标题。"

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
                usage = (note.get("usage") or "").strip()
                scene = f"（场景：{usage}）" if usage else ""
                lines.append(f"{idx}. [{note.get('record_time', '')}] {note.get('text', '')}{scene}")
        else:
            lines.append("记录：无")
        return "\n".join(lines)

    @classmethod
    async def record_note(
        cls,
        text: str,
        source: str,
        session_id: str = "",
        session_name: str = "",
        usage: str = "",
    ) -> str:
        """记录内容进当前事件链的缓存文档，返回 note_id。

        Args:
            text: 要记录的内容（纯文本）。
            source: 记录来源（由调用方注入，如 "conversation"）。
            session_id: 会话 ID（由调用方注入）。
            session_name: 会话名（由调用方注入）。
            usage: 该条记录的自由备注（元数据，完全自由填写，不参与手册路由；query_note 查详情可见）。
        """
        from brain.hc.shortlight import get_shortlight_notepad

        return get_shortlight_notepad().append_note(
            text=text,
            source=source,
            session_id=session_id,
            session_name=session_name,
            usage=usage,
        )
