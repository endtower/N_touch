# -*- coding: utf-8 -*-

"""反思工具；四种临界行动统一交给通用 maintenance_action，普通退出不重置或补偿。"""

import json
from typing import Dict, Any, List
from utils.logger import get_logger
from utils.error_handling import async_handle_errors
from brain.base import ToolManager, ToolHC, ToolBequest, ToolSocial, ToolTask, ToolPersistent, ToolPersona
from brain.hc.shortlight import SCOPE_NORMAL
from brain.hc.global_session_state import get_global_session_state
from brain.base.common_ways.tool_maintenance import ToolMaintenance


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
        # 收尾工具（结束会话 + 可选行动接力，schema 与实现均在本模块）。
        return [
            ToolMaintenance.get_tool_schema(),
            ToolSocial.get_query_history_tool_schema(),
            ToolSocial.get_query_address_book_tool_schema(),
            # memory_agent（复用 ToolPersistent，2026-09-20 接入：复盘发现印象偏差时
            # 经通用层修正记忆——session 会话印象 / mentioned_person 被提及人物）
            ToolPersistent.get_memory_agent_tool_schema(),
            ToolHC.get_list_notes_tool_schema(),
            ToolHC.get_query_note_tool_schema(),
            {
                "type": "function",
                "function": {
                    "name": "record_observation",
                    "description": (
                        "将反思中发现的、值得以后参考的社会现象、行为模式或认识变化写入见闻录。"
                        "见闻录是对一种群体性事件、某种特定话题的具体观察和思考，包含多人的观点和自己最终的态度看法"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "text": {
                                "type": "string",
                                "description": "一小段见闻：观察到的现象和结论",
                            },
                        },
                        "required": ["text"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "archive_notes",
                    "description": (
                        "归档全部已落库笔记："
                        "按落库时间把全部笔记移入按月归档文件夹并退出活动池，"
                        "凡未转化为实际信息的笔记随归档视为自动丢弃。"
                        "本工具不写纪要；如需留档请随后调用 create_reflection_note 自行撰写。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {},
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "create_reflection_note",
                    "description": (
                        "落库一篇反思留档：输入提炼后的 items 生成条目，"
                        "标题自动生成为「<落库时间>的反思留档」，无需提供。"
                        "整篇（标题+全部条目）硬性少于150字，超出部分自动截断，"
                        "条目按输入顺序依次保留。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "items": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "提炼条目列表，每条一句原则/规律/结论，"
                                    "按输入顺序依次保留（超出整篇150字预算的部分被截断丢弃）"
                                ),
                            },
                        },
                        "required": ["items"],
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "finalize_reflection",
                    "description": (
                        "结束本轮反思，需要调用两次。首次只预览行动意图，"
                        "不关闭、不发布行动接力，也不改变情感值。"
                        "临界行动必须通过 maintenance_action 登记；普通行动接力不产生情感重置。"
                        "第二次登记普通行动接力并关闭，不发放补偿。"
                        "第二次省略参数沿用首次的行动意图，也可以传入新参数修改；"
                        "明确 enter_action=false 可取消原来的行动接力。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "enter_action": {
                                "type": "boolean",
                                "description": (
                                    "可选。是否在本次反思结束后进入行动状态，行动阶段可以与其他人物角色展开聊天行动等。"
                                    "设为 true 时必须同时提供 action_purpose；"
                                )
                            },
                            "action_purpose": {
                                "type": "string",
                                "description": (
                                    "可选。进入行动的目的："
                                    "下一轮行动要做什么、要推进什么事务或验证什么，将作为新一轮 PFCA 会话的首条输入。"
                                )
                            },
                        },
                    }
                }
            },
            # 经验登记/删除（schema 与实现统一收口于 ToolBequest，2026-09-15；
            # 2026-09-27 决策经验入口已从 LLM 视野移除，此处只保留话术经验；
            # 2026-10-07 query_bequest 摘除，见模块 docstring 第 9 条）
            ToolBequest.get_upsert_bequest_tool_schema(),
            ToolBequest.get_delete_bequest_tool_schema(),
            # 通用级任务工具（2026-09-18 接入：查询任务状态；
            # 记录写入级 create_task 仍私有于 QuestAgent，不对反思开放）
            *ToolTask.get_task_tool_schemas(),
            # quest_agent 联系通道（2026-09-18 接入：直接联系 quest agent，
            # 转交「标题 + 上下文 + 唤醒时间」由其查重登记；写操作保留在 Quest）
            ToolTask.get_quest_agent_tool_schema(),
            # 基本人格说话风格查询/修改（2026-09-29 接入：反思可查看并调整
            # common 的基本说话风格——发言收敛类调整既可落话术经验也可落该字段；
            # 2026-10-07 收窄为 query/update 两个，schema 收口于通用层 ToolPersona）
            *ToolPersona.get_persona_tool_schemas(),
        ]

    def __init__(self):
        """初始化工具管理器"""
        logger.debug("初始化Reflection_tool_manager工具管理器")
        self._begin_session()
        # 初始化工具映射表，用于动态路由
        self.tool_functions = {
            "maintenance_action": ToolMaintenance.maintenance_action,
            "query_history": ToolSocial.query_history,
            "query_address_book": ToolSocial.query_address_book,
            # memory_agent 直接引用 ToolPersistent 类方法（schema 与实现同源）
            "memory_agent": ToolPersistent.memory_agent,
            "archive_notes": self.archive_notes,
            "create_reflection_note": self.create_reflection_note,
            "record_observation": self.record_observation,
            "finalize_reflection": self.finalize_reflection,
            "query_note": self.query_note,
            "list_notes": self.list_notes,
            "upsert_bequest": self.upsert_bequest,
            "delete_bequest": self.delete_bequest,
            # 通用级任务工具（直接引用 ToolTask 类方法）
            "query_task_status": ToolTask.query_task_status,
            # Reflection 专属用途包装，底层接续记录仍由 Quest 整理。
            "quest_agent": ToolTask.quest_agent,
            # 基本人格说话风格查询/修改（包装委托 ToolPersona）
            "query_common_persona": self.query_common_persona,
            "update_common_persona": self.update_common_persona,
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
        """按章节标题查询一篇普通笔记的完整细节，委托通用 ToolHC.query_note。

        章节标题取自 list_notes 的普通笔记返回；见闻录 / 任务相关笔记等
        特殊槽位由 list_notes 直接给全，不经此工具。"""
        return await ToolHC.query_note(title)

    async def list_notes(self, scope: str = SCOPE_NORMAL) -> str:
        """列出笔记概览，委托通用 ToolHC.list_notes。

        scope 可选普通笔记（默认，已落库事件链文档，给章节标题与条目数）/
        见闻录 / 任务相关笔记（后两者为特殊储存槽位台账，逐条给全）。"""
        return await ToolHC.list_notes(scope=scope)

    async def record_observation(self, text: str) -> str:
        """独立记录见闻，正文与自动元数据由记事本存储层持久化。"""
        from brain.hc.shortlight import get_shortlight_notepad

        note_id = await get_shortlight_notepad().record_observation(text)
        return f"见闻已记录：{note_id}。可通过 list_notes(scope='见闻录') 查阅。"

    async def upsert_bequest(
        self, title: str, summary: str, category: str = "话术", persona: str = ""
    ) -> Dict[str, Any]:
        """登记经验（创建/覆盖），委托通用 ToolBequest.upsert_bequest。
        按 title + category 精确匹配定位：命中则覆盖内容，否则新建；裁决逻辑（登记什么、
        如何措辞、归入哪一类、是否打人格标签）由反思会话本身负责，本方法只做落库执行。
        category 为中文枚举「话术」/「决策」，缺省「话术」；persona 为可选人格标签
        （2026-10-01 起：仅当经验明确只属于某个人格的应对方式时传，通用技巧不传；
        人格名不存在会被硬拦）。"""
        return await ToolBequest.upsert_bequest(
            title=title, summary=summary, category=category, persona=persona
        )

    async def delete_bequest(self, title: str, category: str = "话术") -> Dict[str, Any]:
        """删除经验（按 title + category 精确匹配），委托通用 ToolBequest.delete_bequest。
        何时删除（经验被认为不恰当）由反思会话裁决，本方法只做落库执行。
        category 为中文枚举「话术」/「决策」，缺省「话术」。"""
        return await ToolBequest.delete_bequest(title=title, category=category)

    async def query_common_persona(self) -> Dict[str, Any]:
        """查询基本人格（common）当前的基本说话风格全文，委托通用 ToolPersona。
        复盘时供反思会话对照现状，判断是否调整说话底线。"""
        return ToolPersona.query_common_persona()

    async def update_common_persona(self, kernel_prompt: str) -> Dict[str, Any]:
        """覆盖式更新基本人格（common）的基本说话风格并落盘，委托通用 ToolPersona。
        何时调整、调整为谁由反思会话裁决，本方法只做落库执行。"""
        return await ToolPersona.update_common_persona(kernel_prompt=kernel_prompt)

    async def archive_notes(self) -> str:
        """一次性归档全部已落库笔记（2026-09-23 拆出）。

        执行序（任一步失败直接上抛崩溃阻断，不吞）：
        archive_all 按落库时间把全部笔记移入 notes/archive/<YYYY-MM>/
        并退出活动池，未转化为实际信息者视为自动丢弃。

        本工具不写纪要：留档由反思系统自行调用 create_reflection_note
        撰写（内容裁决权在反思会话）。

        Returns:
            str: 归档结果（逐篇标题 + 归档路径），并附留档提示。
        """
        from brain.hc.shortlight import get_shortlight_notepad

        notepad = get_shortlight_notepad()

        archived = await notepad.archive_all()
        if not archived:
            return "活动池为空，无笔记可归档。"

        lines = [f"已归档 {len(archived)} 篇旧笔记（未转化为实际信息者视为自动丢弃）："]
        for r in archived:
            lines.append(f"- {r['title']}（{r['file']} → {r['dest']}）")
        lines.append("如需留档，请调用 create_reflection_note 自行撰写反思留档（整篇须少于150字）。")
        return "\n".join(lines)

    async def create_reflection_note(self, items: List[str]) -> str:
        """落库一篇反思留档（2026-09-23 新增）。

        标题自动生成为「<落库时间>的反思留档」，items 依序成为条目；
        整篇（标题+全部条目）硬性少于 150 字（notepad.REFLECTION_NOTE_MAX_CHARS），
        超出部分自动截断不驳回；items 为空或清洗后无有效条目抛 ValueError
        （走 LLM 自纠通道）。落库细节由 notepad.create_document 收口。
        """
        from brain.hc.shortlight import get_shortlight_notepad

        notepad = get_shortlight_notepad()
        name = await notepad.create_document(notes=items)
        return f"反思留档已落库：{name}（标题自动生成，整篇少于150字，超出部分已截断）"

    def _begin_session(self) -> None:
        self._finalize_pending = None

    async def finalize_reflection(self, enter_action: bool | None = None,
                                  action_purpose: str | None = None) -> Any:
        """两次确认普通退出/行动接力；临界行动统一由 maintenance_action 处理。"""
        previous = self._finalize_pending or {"enter_action": False, "action_purpose": ""}
        enter_action = previous["enter_action"] if enter_action is None else enter_action
        action_purpose = previous["action_purpose"] if action_purpose is None else action_purpose
        if not isinstance(enter_action, bool):
            raise ValueError("enter_action 必须为布尔值")
        if not isinstance(action_purpose, str) or (enter_action and not action_purpose.strip()):
            raise ValueError("进入行动时必须说明 action_purpose")
        plan = {"enter_action": enter_action, "action_purpose": action_purpose.strip()}
        if self._finalize_pending is None:
            self._finalize_pending = plan
            return {"confirmation_required": True, "pending_action": plan,
                    "message": "首次结束预览，不改变情感值。四种临界行动请使用 maintenance_action；再次调用确认结束。"}
        if enter_action:
            get_global_session_state()._data["pfc_handoff"] = {"state": "action", "purpose": plan["action_purpose"]}
        from brain.pfc.reflection.reflection_core import get_reflection_instance
        await get_reflection_instance().stop(exit_reason="reflection_done")
        return "反思已结束。"


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
