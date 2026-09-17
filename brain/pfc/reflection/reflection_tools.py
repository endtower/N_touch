# -*- coding: utf-8 -*-

"""
reflection模块的工具定义与处理（针对反思场景重新设计）

与 pfca_tools 的定位差异：
- pfca 是行动调度器：工具面向"对外行动"（启动对话 agent、调度决策 agent）。
- reflection 是内省复盘器：工具面向"回顾事实 → 检验认知 → 落地结论"，
  不包含任何对外行动类工具（无 conversation_agent / social_chat_agent /
  advanced_decision_agent）；印象修正与任务登记也不由反思直接执行
  （memory_agent / task_register_agent 已于 2026-09-15 移出反思工具集）。

工具集（schema 与实现尽量收口于 brain/base/common_ways，本模块只留反思专属工具）：
1. query_history / query_address_book（复用 ToolSocial）：回看聊天记录核实事实、
   查询通讯录/角色印象检验认知偏差；consumer_id 默认 None＝纯只读，不推进任何未读游标。
2. list_notes / query_note（复用 ToolHC）：查阅记事本活动池笔记，验证历史经验。
3. finalize_reflection（反思专属，2026-09-16）：一次性收尾工具，自动完成笔记消费闭环——
   归档全部已落库笔记（按落库时间移入 notes/archive/<YYYY-MM>/ 并退出活动池，
   未转化为实际信息者视为自动丢弃）→ 创建本轮反思的新笔记（source="reflection"，
   独立落库不经过事件链缓存，立即进入活动池）→ summary 写入短期记忆并结束会话。
   （自 create_new_note / archive_notes / terminate 三工具融合而来，归档不再由
   LLM 手动调用，避免漏归档/顺序颠倒。）
4. upsert_bequest / delete_bequest（收口于 ToolBequest）：话术经验的登记（创建/覆盖）与删除。

会话标识统一使用 session_id，格式为 "type_id"，例如 "private_1234567890" / "group_198431299"。
"""

import json
from typing import Dict, Any, List
from utils.logger import get_logger
from utils.error_handling import async_handle_errors
from brain.base import ToolManager, ToolHC, ToolBequest, ToolSocial
from brain.hc.global_session_state import get_global_session_state


# 获取日志记录器
logger = get_logger('ReflectionTools', "INFO")


class Reflection_tool_manager(ToolManager):
    """Reflection工具管理器"""

    @staticmethod
    def get_tools() -> List[Dict[str, Any]]:
        """
        获取可用的工具列表，用于LLM的function call

        Returns:
            List[Dict[str, Any]]: 工具列表，每个工具包含名称、描述和参数信息
        """
        logger.debug("获取可用工具列表")
        # query_history / query_address_book 的 schema 与实现统一复用 ToolSocial
        # （consumer_id 默认 None＝纯只读，不推进任何未读游标）；list_notes /
        # query_note schema 复用 ToolHC 通用版；finalize_reflection 为反思专属
        # 收尾工具（笔记消费闭环 + 退出，schema 与实现均在本模块）。
        return [
            ToolSocial.get_query_history_tool_schema(),
            ToolSocial.get_query_address_book_tool_schema(),
            ToolHC.get_list_notes_tool_schema(),
            ToolHC.get_query_note_tool_schema(),
            {
                "type": "function",
                "function": {
                    "name": "finalize_reflection",
                    "description": (
                        "结束本轮反思（一次性收尾，反思的最后一步）：自动完成三件事——"
                        "①将全部已落库笔记按落库时间移入按月归档文件夹并退出活动池"
                        "（凡未转化为实际信息的笔记随归档视为自动丢弃，归档自动执行，"
                        "无需手动调用）；②创建本轮反思的新笔记并落库、立即进入活动池"
                        "（作为下次唤醒时的活动笔记）；③将 summary 写入短期记忆并结束"
                        "反思会话。请在确认所有结论（如话术经验登记）均已落地后再调用，"
                        "调用后反思即结束，不可继续使用其他工具。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": "新笔记标题（简洁概括主题）。"
                            },
                            "items": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "提炼后的条目列表，每条 40 字以内——一句以后能直接复用的话，"
                                    "只记规则（遇到什么情况→怎么做），不记事件经过与对话复述。"
                                ),
                            },
                            "summary": {
                                "type": "string",
                                "description": (
                                    "本次反思的总结（一语两用）：完整前因后果、判断失误/验证、"
                                    "得出的教训（需包含具体人物名称）；随新笔记落库存档，"
                                    "同时写入短期记忆供下次唤醒快速回顾。"
                                ),
                            },
                        },
                        "required": ["title", "items", "summary"]
                    }
                }
            },
            # 话术经验登记/删除（schema 与实现统一收口于 ToolBequest，2026-09-15）
            ToolBequest.get_upsert_bequest_tool_schema(),
            ToolBequest.get_delete_bequest_tool_schema(),
        ]

    def __init__(self):
        """初始化工具管理器"""
        logger.debug("初始化Reflection_tool_manager工具管理器")
        # 初始化工具映射表，用于动态路由
        self.tool_functions = {
            "query_history": ToolSocial.query_history,
            "query_address_book": ToolSocial.query_address_book,
            "finalize_reflection": self.finalize_reflection,
            "query_note": self.query_note,
            "list_notes": self.list_notes,
            "upsert_bequest": self.upsert_bequest,
            "delete_bequest": self.delete_bequest,
        }

    @async_handle_errors()
    async def handle_tool_call(self, tool_call) -> Dict[str, Any]:
        """
        处理LLM的工具调用请求
        作为工具调用的中央路由，负责将工具调用分发到对应的处理函数

        错误处理策略：
        - AbyssCoreError 视为"不可恢复"（程序/系统级故障）→ 仍走 os._exit 强制退出。
        - 参数错误（参数解析失败 / 工具名拼错 / 工具内部参数错误）按"可恢复错误"处理：
          构造一条 role=tool 的错误消息返回给 LLM，让其自纠。
        - 其余异常（模块缺失 / IO 错等）→ 上抛不吞，直接崩溃阻断，防止错误扩大蔓延。

        Args:
            tool_call: 工具调用请求信息

        Returns:
            Dict[str, Any]: 工具调用的结果；参数错误时返回 role=tool 的错误消息。
        """
        # —— 1. 解析 tool_call 形态 ——
        parsed = self._parse_tool_call(tool_call)
        if parsed is None:
            return self._tool_error_response(
                tool_call_id=getattr(tool_call, 'id', None) if hasattr(tool_call, 'id') else (tool_call.get('id') if isinstance(tool_call, dict) else None),
                tool_name="<unknown>",
                err=ValueError(f"无法解析工具调用格式: {tool_call}"),
            )
        tool_name = parsed["name"]
        tool_args_json = parsed["arguments"]
        tool_call_id = parsed["id"]

        # —— 2. 参数解析容错（LLM 偶尔吐 Python dict 字面量而非合法 JSON）——
        try:
            tool_args = self._parse_arguments(tool_args_json)
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.warning(f"工具 {tool_name} 参数解析失败: {e}; raw={tool_args_json!r}")
            return self._tool_error_response(tool_call_id, tool_name, e)

        # —— 3. 工具名查表 ——
        if tool_name not in self.tool_functions:
            err = ValueError(f"未知工具: {tool_name}")
            logger.error(str(err))
            return self._tool_error_response(tool_call_id, tool_name, err)

        tool_function = self.tool_functions[tool_name]

        # —— 4. 前端同步: 每次工具调用都把动作广播给 web 通道 ——
        # (后端维护单一 activity 状态, 前端只渲染)
        get_global_session_state().set_activity(
            source="reflection", tool_name=tool_name, tool_args=tool_args
        )

        # —— 5. 执行工具函数；统一错误处理（参数错误回 LLM，其余异常上抛阻断）——
        result = await self._safe_execute(
            tool_function, tool_call_id, tool_name, **tool_args
        )
        if isinstance(result, dict) and result.get("role") == "tool":
            # 出错时 _safe_execute 已返回 role=tool 的错误消息，直接透传
            return result

        content = json.dumps(result, ensure_ascii=False) if isinstance(result, (dict, list)) else str(result)

        return {
            "role": "tool",
            "name": tool_name,
            "content": content,
            "tool_call_id": tool_call_id,
        }

    async def query_note(self, title: str) -> str:
        """查询记事本某一章节（按标题精确匹配，重名取最新）的完整细节，
        委托通用 ToolHC.query_note（起因/总结/自我评价/逐条记录及元数据）。"""
        return await ToolHC.query_note(title)

    async def list_notes(self) -> str:
        """列出记事本全部已落库笔记概览，委托通用 ToolHC.list_notes
        （标题/触发原因摘要/落库时间/记录条数；为 query_note 提供标题来源）。"""
        return await ToolHC.list_notes()

    async def upsert_bequest(self, title: str, summary: str) -> Dict[str, Any]:
        """登记话术经验（创建/覆盖），委托通用 ToolBequest.upsert_bequest。
        按 title 精确匹配定位：命中则覆盖内容，否则新建；裁决逻辑（登记什么、
        如何措辞）由反思会话本身负责，本方法只做落库执行。
        category 固定 'speech'（话术专用），工具不暴露分类参数。"""
        return await ToolBequest.upsert_bequest(title=title, summary=summary)

    async def delete_bequest(self, title: str) -> Dict[str, Any]:
        """删除话术经验（按 title 精确匹配），委托通用 ToolBequest.delete_bequest。
        何时删除（经验被认为不恰当）由反思会话裁决，本方法只做落库执行。"""
        return await ToolBequest.delete_bequest(title=title)

    async def finalize_reflection(self, title: str, items: List[str], summary: str) -> str:
        """结束本轮反思（一次性收尾）——笔记消费闭环与退出动作的融合端。

        自动按序执行（任一步失败即中断上抛：参数错误回 LLM 自纠，系统级错误
        崩溃阻断；已完成的归档/落库状态保留，修正后重试幂等无害）：
        1. 自动归档：全部已落库笔记按落库时间移入 notes/archive/<YYYY-MM>/，
           退出活动池；未转化为实际信息的笔记随归档视为自动丢弃。
        2. 创建新笔记：本轮提炼条目（source="reflection"）独立落库，立即进入
           活动池，作为下次唤醒时的活动笔记。
        3. 总结写入短期记忆 + 设置退出标志，结束会话。
        """
        from brain.hc.shortlight import get_shortlight_notepad
        from brain.hc.temporary_memory import get_temporary_memory
        from brain.pfc.reflection.reflection_core import get_reflection_instance

        # 1. 自动归档全部旧笔记
        archived = await get_shortlight_notepad().archive_all()

        # 2. 创建本轮反思的新笔记（title 空/条目空/单条超 40 字由 notepad 抛 ValueError 回 LLM 自纠）
        name = await get_shortlight_notepad().create_document(
            title=title,
            notes=items,
            summary=summary,
        )

        # 3. 总结写入短期记忆并退出（关键闭环，失败直接上抛崩溃阻断，不吞）
        get_temporary_memory().add(summary)
        reflection_core = get_reflection_instance()
        await reflection_core.stop(exit_reason="reflection_done")

        logger.info(
            f"反思收尾完成: 归档 {len(archived)} 篇旧笔记, 新笔记 {name}, 退出标志已设置"
        )
        lines = [
            f"反思已结束：归档旧笔记 {len(archived)} 篇"
            f"（未转化为实际信息者视为自动丢弃），"
            f"新笔记已创建并落库：{name}（{len(items)} 条记录），"
            f"summary 已写入短期记忆。"
        ]
        for r in archived:
            lines.append(f"- 归档: {r['title']}（{r['file']} → {r['dest']}）")
        return "\n".join(lines)


# 全局Reflection工具管理器实例，延迟初始化
_global_reflection_tool_manager = None


def get_reflection_tool_manager():
    """
    获取Reflection工具管理器实例的方法，实现单例模式

    Returns:
        Reflection_tool_manager: Reflection工具管理器实例
    """
    global _global_reflection_tool_manager
    if _global_reflection_tool_manager is None:
        _global_reflection_tool_manager = Reflection_tool_manager()
    return _global_reflection_tool_manager


# 导出常用函数，作为模块的公共接口
async def handle_tool_call(tool_call) -> Dict[str, Any]:
    """处理工具调用的便捷函数

    作为模块的主要入口点，负责处理来自reflection_core的工具调用请求
    """
    # 获取工具管理器实例
    tool_manager = get_reflection_tool_manager()
    return await tool_manager.handle_tool_call(tool_call)


def get_available_tools() -> List[Dict[str, Any]]:
    """
    获取可用的工具列表，用于LLM的function call

    Returns:
        List[Dict[str, Any]]: 工具列表，每个工具包含名称、描述和参数信息
    """
    return Reflection_tool_manager.get_tools()
