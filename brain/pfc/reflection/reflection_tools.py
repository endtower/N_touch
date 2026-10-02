# -*- coding: utf-8 -*-

"""
reflection模块的工具定义与处理（针对反思场景重新设计）

与 pfca_tools 的定位差异：
- pfca 是行动调度器：工具面向"对外行动"（启动对话 agent、召唤内心人格）。
- reflection 是内省复盘器：工具面向"回顾事实 → 检验认知 → 落地结论"，
  不包含任何对外行动类工具（无 conversation_agent / social_chat_agent /
  inner_persona_agent）；任务裁决独占于 quest agent，反思仅可经
  quest_agent 联系通道转交，2026-09-18 接入。

工具集（schema 与实现尽量收口于 brain/base/common_ways，本模块只留反思专属工具）：
1. query_history / query_address_book（复用 ToolSocial）：回看聊天记录核实事实、
   查询通讯录/角色印象检验认知偏差；consumer_id 默认 None＝纯只读，不推进任何未读游标。
2. memory_agent（复用 ToolPersistent，2026-09-20 重新接入）：反思复盘中发现
   对目标的基础信息印象存在偏差/更新时，经通用层修正印象（写入短期记忆与通讯录）。
3. list_notes / query_note（复用 ToolHC）：查阅记事本活动池笔记，验证历史经验。
4. finalize_reflection（反思专属，2026-09-16；2026-09-23 收口）：一次性收尾工具，
   只做一件事——结束会话；可选接力 enter_action 进入行动状态（由 run_pfc_cycle
   消费 pfc_handoff）。summary 写入与笔记整理（归档/建新笔记）均从收尾中移除：
   归档由 archive_notes 工具由 LLM 自主决定何时执行，不再随收尾自动发生。
5. archive_notes（反思专属，2026-09-23 拆出）：一次性归档全部已落库笔记（不自动
   写纪要）；如需留档由反思系统经 create_reflection_note 自行撰写。
6. create_reflection_note（反思专属，2026-09-23 新增）：落库一篇反思留档——
   输入 items 生成条目，标题自动生成为「<落库时间>的反思留档」；整篇
   （标题+条目+总结）硬性少于 150 字（notepad.REFLECTION_NOTE_MAX_CHARS），
   超出自动截断不驳回。
7. append_task_log / query_task_status（复用 ToolTask，2026-09-18 接入）：查询当前
   任务状态；反思中与任务相关的观察/结论，以记录级笔记追加给对应任务，等待
   quest agent 整理归位（反思自身不做任务裁决：不立项、不改阶段、不完结）。
8. quest_agent（复用 ToolTask，2026-09-18 接入）：联系 quest agent 的入口通道——
   转交一段内容请它裁决（审批推进 / 登记任务 / 修订任务）；是否立项、能否推进
   由 quest agent 独立裁决，反思不持有裁决权。
9. query_bequest / upsert_bequest / delete_bequest（收口于 ToolBequest）：
   话术经验（发言/回复技巧）按语义相似度检索，增删经 category 参数（中文枚举「话术」）定向。
   决策经验相关入口（query_decision_bequest，以及 upsert/delete 的「决策」类别）已于
   2026-09-27 从 LLM 视野移除；ToolBequest 的通用实现保留，供程序侧直接调用。
10. list_personas / query_persona / update_persona（收口于 ToolPersona，2026-09-29 接入；
    2026-10-01 起分层冻结）：人格定义查询与修改——普通人格内核已冻结，仅基本人格
    （common）可经 update_persona 修改，其 kernel_prompt 语义收窄为「基本说话风格」
    （说人话式普适要求，硬预算 300 字）；覆盖写入自动留档旧版可回滚。
    与话术经验（bequest）并列，同属反思"调整既有人格预设/经验"的两类落点。
    upsert_bequest 自 2026-10-01 起支持可选 persona 人格标签（观察期：仅落库）。

会话标识统一使用 session_id，格式为 "type_id"，例如 "private_1234567890" / "group_198431299"。
"""

import json
from typing import Dict, Any, List
from utils.logger import get_logger
from utils.error_handling import async_handle_errors
from brain.base import ToolManager, ToolHC, ToolBequest, ToolSocial, ToolTask, ToolPersistent, ToolPersona
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
        # 收尾工具（结束会话 + 可选行动接力，schema 与实现均在本模块）。
        return [
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
                        "结束本轮反思"
                        "可以选择直接进入行动状态，与其他人聊天、执行任务或行动 "
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "enter_action": {
                                "type": "boolean",
                                "description": (
                                    "可选。是否在本次反思结束后进入行动状态，行动阶段可以与其他人物角色展开聊天行动等。"
                                    "设为 true 时必须同时提供 action_purpose。"
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
            # 经验检索/登记/删除（schema 与实现统一收口于 ToolBequest，2026-09-15；
            # 2026-09-27 决策经验入口已从 LLM 视野移除，此处只保留话术经验工具；
            # 2026-09-29 query_bequest 额外暴露「检索方式」：向量 / 标题比对，
            # 标题比对命中少、省 token）
            ToolBequest.get_query_bequest_tool_schema(mode_selectable=True),
            ToolBequest.get_upsert_bequest_tool_schema(),
            ToolBequest.get_delete_bequest_tool_schema(),
            # 通用级任务工具（2026-09-18 接入：查询任务状态 + 给任务添加笔记；
            # 裁决级 create_task / update_task_status 仍私有于 QuestAgent，不对反思开放）
            *ToolTask.get_task_tool_schemas(),
            # quest_agent 联系通道（2026-09-18 接入：直接联系 quest agent，转交内容
            # 由其裁决——审批推进 / 登记任务 / 修订任务；反思不持有裁决权）
            ToolTask.get_quest_agent_tool_schema(),
            # 人格定义查询/修改（2026-09-29 接入：反思可查看并调整人格预设——人格是
            # "应对特定方面事件的状态"，发言收敛类调整既可落话术经验也可落人格定义；
            # schema 收口于通用层 ToolPersona）
            *ToolPersona.get_persona_tool_schemas(),
        ]

    def __init__(self):
        """初始化工具管理器"""
        logger.debug("初始化Reflection_tool_manager工具管理器")
        # 初始化工具映射表，用于动态路由
        self.tool_functions = {
            "query_history": ToolSocial.query_history,
            "query_address_book": ToolSocial.query_address_book,
            # memory_agent 直接引用 ToolPersistent 类方法（schema 与实现同源）
            "memory_agent": ToolPersistent.memory_agent,
            "archive_notes": self.archive_notes,
            "create_reflection_note": self.create_reflection_note,
            "finalize_reflection": self.finalize_reflection,
            "query_note": self.query_note,
            "list_notes": self.list_notes,
            "upsert_bequest": self.upsert_bequest,
            "delete_bequest": self.delete_bequest,
            "query_bequest": self.query_bequest,
            # 通用级任务工具（直接引用 ToolTask 类方法）
            "append_task_log": ToolTask.append_task_log,
            "query_task_status": ToolTask.query_task_status,
            # quest_agent 联系通道（直接引用 ToolTask 类方法）
            "quest_agent": ToolTask.quest_agent,
            # 人格定义查询/修改（包装委托 ToolPersona）
            "list_personas": self.list_personas,
            "query_persona": self.query_persona,
            "update_persona": self.update_persona,
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

    async def query_bequest(
        self, query: str, top_k: int = 3, mode: str = "向量"
    ) -> Dict[str, Any]:
        """查询话术经验，委托通用 ToolBequest.query_bequest。
        mode 「向量」（默认）按语义相似度返回最相似的经验（标题 + 内容 + 相似度）；
        「标题」按标题关键词比对，命中更少、省 token，未命中时返回 hint 提示改用向量。
        复盘时供反思会话对照既往话术、验证或提炼结论。"""
        return await ToolBequest.query_bequest(query=query, top_k=top_k, mode=mode)

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

    async def list_personas(self) -> Dict[str, Any]:
        """列出全部人格（含基本人格），委托通用 ToolPersona.list_personas。
        复盘时供反思会话掌握现有人格清单，判断是否需要调整某个人格预设。"""
        return ToolPersona.list_personas()

    async def query_persona(self, persona_name: str) -> Dict[str, Any]:
        """查询一个人格的场景梗概与人格定义，委托通用 ToolPersona.query_persona。
        人格不存在抛 ValueError 走 LLM 自纠通道。"""
        return ToolPersona.query_persona(persona_name=persona_name)

    async def update_persona(self, persona_name: str, kernel_prompt: str) -> Dict[str, Any]:
        """覆盖式更新一个人格的定义并落盘，委托通用 ToolPersona.update_persona。
        何时调整、调整为谁由反思会话裁决，本方法只做落库执行。"""
        return await ToolPersona.update_persona(persona_name=persona_name, kernel_prompt=kernel_prompt)

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

    async def finalize_reflection(self, enter_action: bool = False,
                                  action_purpose: str = "") -> str:
        """结束本轮反思（一次性收尾）——退出 + 可选行动接力。

        自动按序执行（任一步失败即中断上抛：参数错误回 LLM 自纠，系统级错误
        崩溃阻断）：
        1. 设置退出标志，结束会话。
        2. 可选接力：enter_action=true 时登记行动接力请求（由 core.py run_pfc_cycle
           消费），反思结束后启动新一轮 PFCA 会话，把反思结论转化为行动。

        注意：笔记归档/创建已从收尾中拆出（archive_notes 工具由 LLM 自主调用），
        本工具不再自动整理笔记、不再写短期记忆总结；需要归档旧笔记请在调用本工具
        前先调 archive_notes。
        """
        from brain.pfc.reflection.reflection_core import get_reflection_instance

        # 参数校验前置（在任何副作用之前）：enter_action=true 时 action_purpose
        # 必填，缺失属参数错误，抛 ValueError 交 LLM 自纠（_safe_execute 可恢复
        # 错误通道）。
        if enter_action:
            if not (action_purpose or "").strip():
                raise ValueError("enter_action=true 时必须提供 action_purpose")

        reflection_core = get_reflection_instance()
        await reflection_core.stop(exit_reason="reflection_done")

        # 可选接力：反思结束后进入行动状态（由 core.py run_pfc_cycle 消费）
        if enter_action:
            purpose = (action_purpose or "").strip()
            get_global_session_state()._data["pfc_handoff"] = {
                "state": "action",
                "purpose": purpose,
            }
            logger.info("finalize_reflection 已登记行动接力，反思结束后进入行动状态")

        logger.info("反思收尾完成: 退出标志已设置")
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
