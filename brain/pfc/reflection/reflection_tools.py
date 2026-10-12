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
   list_notes 按 scope 取概览（普通笔记给章节标题与条目数，见闻录 / 任务相关笔记
   逐条给全）；query_note 仅按章节标题回溯普通笔记详情。
   record_observation（反思专属）：将实际交互中发现的现象及依据独立写入见闻录。
4. finalize_reflection（反思专属，2026-10-05 改为二次确认）：首次预览低值与验证安排，
   第二次结束会话；可选接力 enter_action 进入行动状态（由 run_pfc_cycle
   消费 pfc_handoff）。summary 写入与笔记整理（归档/建新笔记）均从收尾中移除：
   归档由 archive_notes 工具由 LLM 自主决定何时执行，不再随收尾自动发生。
5. archive_notes（反思专属，2026-09-23 拆出）：一次性归档全部已落库笔记（不自动
   写纪要）；如需留档由反思系统经 create_reflection_note 自行撰写。
6. create_reflection_note（反思专属，2026-09-23 新增）：落库一篇反思留档——
   输入 items 生成条目，标题自动生成为「<落库时间>的反思留档」；整篇
   （标题+条目+总结）硬性少于 150 字（notepad.REFLECTION_NOTE_MAX_CHARS），
   超出自动截断不驳回。
7. query_task_status（复用 ToolTask，2026-09-18 接入）：查询当前任务状态；反思自身
   不登记任务，需要安排接续时经 quest_agent 转交。
8. quest_agent（委托 ToolTask，2026-10-05 增加 Reflection 专属用途）：联系 quest agent 的入口通道——
   转交「标题 + 要传递给下次启动时的上下文 + 下次唤醒时间」，由 Quest 查重后登记
   （重复则不登记）；是否登记由 Quest 独立判断，反思不持有写权限；
   实际登记后按用途补偿效能感或归属感。
9. upsert_bequest / delete_bequest（收口于 ToolBequest）：
   话术经验（发言/回复技巧）的登记与删除，经 category 参数（中文枚举「话术」）定向。
   query_bequest 已于 2026-10-07 从反思工具表摘除：反思不对外发言，「说话前找技巧」
   不适用，而「登记前查重」已由 upsert_bequest 落库前自动完成（返回相似经验与重复警告），
   主动检索基本是冗余空转，且结果与首条注入的记事本手册高度重叠。
   决策经验相关入口（query_decision_bequest，以及 upsert/delete 的「决策」类别）已于
   2026-09-27 从 LLM 视野移除；ToolBequest 的通用实现保留，供程序侧直接调用。
10. query_common_persona / update_common_persona（收口于 ToolPersona，2026-09-29 接入；
    2026-10-07 收窄为两个）：查询与覆盖式修改基本人格（common）的「基本说话风格」
    （说人话式普适要求，硬预算 300 字）——这是普通人格预设外唯一可由 LLM 经工具
    改动的字段；普通人格内核已冻结，原 list_personas / query_persona / update_persona
    三个人格工具已移除。覆盖写入自动留档旧版可回滚。
    与话术经验（bequest）并列，同属反思"调整既有人格预设/经验"的两类落点。
    upsert_bequest 自 2026-10-01 起支持可选 persona 人格标签（观察期：仅落库）。

会话标识统一使用 session_id，格式为 "type_id"，例如 "private_1234567890" / "group_198431299"。
"""

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

PURPOSE_GENERAL = "普通事务"
PURPOSE_SELF_VERIFICATION = "自修正验证"
PURPOSE_SOCIAL = "主动社交"
PURPOSE_SOCIAL_GROWTH = "关系建设"


def _purpose_parameter(allow_growth: bool = True) -> Dict[str, Any]:
    return {
        "type": "string",
        "enum": [PURPOSE_GENERAL, PURPOSE_SELF_VERIFICATION, PURPOSE_SOCIAL] + ([PURPOSE_SOCIAL_GROWTH] if allow_growth else []),
        "default": PURPOSE_GENERAL,
        "description": (
            "本次安排的用途，默认普通事务。自修正验证用于尝试调整后的处理方式，"
            "成功安排后补偿效能感 +30；主动社交用于主动联系、交流、寻求回应或支持，"
            "成功安排后补偿归属感 +30。关系建设用于归属感达到100时主动积累社交合作条件，"
            "仅限经 Quest 择时登记，成功登记后归属感重置为50，不给+30补偿。"
            "在 content 或 action_purpose 中说明对象、目的与准备做什么。"
            "两项分别在同一轮反思最多补偿一次，一次安排只补偿所选用途对应的指标。"
        ),
    }


class Reflection_tool_manager(ToolManager):
    """Reflection工具管理器"""

    @staticmethod
    def _get_quest_agent_schema() -> Dict[str, Any]:
        schema = ToolTask.get_quest_agent_tool_schema()
        schema["function"]["parameters"]["properties"]["purpose_type"] = _purpose_parameter()
        schema["function"]["description"] += (
            "自修正验证或主动社交只在 Quest 实际成功登记新任务后获得对应补偿；"
            "仅咨询、驳回或执行失败不补偿。"
        )
        return schema

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
            ToolMaintenance.get_query_maintenance_tool_schema(),
            ToolMaintenance.get_request_maintenance_tool_schema(),
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
                        "依据实际交互、笔记或查阅的聊天记录，写一小段：我注意到了什么，"
                        "哪些言论或行为让我这样认为。必须限定观察范围，"
                        "部分人的表现不得扩大为整个群体或市场；推测须明确写成推测。"
                        "禁止只写空泛感想或照抄聊天摘要、人物资料、任务进度；"
                        "没有新发现时不必调用，不为完成反思编造见闻。"
                        "可先用 list_notes(scope='见闻录') 查阅已有内容，避免重复记录。"
                        "独立保存，不受普通笔记60字或反思留档150字的截断限制，不自动生成任务或锦囊。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "text": {
                                "type": "string",
                                "description": "一小段见闻：观察到的现象及实际依据；解释或推测明确标注，范围与依据一致。",
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
                        "结束本轮反思，需要调用两次。首次只预览当前情感与验证安排，"
                        "提示未安排对应行动且低值持续时可能再次启动反思，不关闭、不发布、不加分。"
                        "你可以先调整或安排自修正验证、主动社交，再第二次调用确认结束。"
                        "第二次执行行动安排并关闭，即使情感仍低或没有验证也会结束，不再要求确认。"
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
                                    "用途为自修正验证或主动社交时必须设为 true。"
                                )
                            },
                            "action_purpose": {
                                "type": "string",
                                "description": (
                                    "可选。进入行动的目的："
                                    "下一轮行动要做什么、要推进什么事务或验证什么，将作为新一轮 PFCA 会话的首条输入。"
                                )
                            },
                            "purpose_type": _purpose_parameter(False),
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
            Reflection_tool_manager._get_quest_agent_schema(),
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
            "query_maintenance": ToolMaintenance.query_maintenance,
            "request_maintenance": ToolMaintenance.request_maintenance,
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
            "quest_agent": self.quest_agent,
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
        """每轮复位；两个发布入口共享资格，效能感与归属感分别最多补偿一次。"""
        self._verification_compensated = False
        self._verification_registered = False
        self._social_compensated = False
        self._social_registered = False
        self._finalize_pending = None

    @staticmethod
    def _validate_purpose(purpose_type: str) -> None:
        if purpose_type not in (PURPOSE_GENERAL, PURPOSE_SELF_VERIFICATION, PURPOSE_SOCIAL, PURPOSE_SOCIAL_GROWTH):
            raise ValueError("purpose_type 必须为普通事务、自修正验证、主动社交或关系建设")

    async def _compensate_once(self, title: str, reason: str,
                               purpose_type: str = PURPOSE_SELF_VERIFICATION) -> Dict[str, Any]:
        if purpose_type == PURPOSE_SOCIAL_GROWTH:
            raise ValueError("关系建设不领取低值补偿")
        from brain.amy.emotional_interface import get_emotional_interface

        ei = await get_emotional_interface()
        social = purpose_type == PURPOSE_SOCIAL
        axis = "recognition" if social else "competence"
        flag = "_social_compensated" if social else "_verification_compensated"
        if getattr(self, flag):
            return {
                "compensation_applied": False,
                "compensation_note": f"本轮反思已给予{purpose_type}补偿，不重复加分。",
                "current_emotion": await ei.get_reflection_description(),
            }
        description = await ei.apply_reflection_compensation(title, reason, axis=axis)
        setattr(self, flag, True)
        return {"compensation_applied": True, "current_emotion": description}

    async def quest_agent(self, title: str, context: str, next_occurrence: str,
                          purpose_type: str = PURPOSE_GENERAL) -> Any:
        """检查重复并登记一条待接续事项；自修正验证或主动社交依据实际登记补偿对应指标。"""
        self._validate_purpose(purpose_type)
        if not isinstance(title, str) or not title.strip():
            raise ValueError("转交 Quest 的 title 不能为空")
        if purpose_type == PURPOSE_SOCIAL_GROWTH:
            from brain.hc.maintenance import MaintenanceService
            service = MaintenanceService()
            eligibility = (await service.query())["eligibility"]["social_growth"]
            if not eligibility["allowed"]:
                return {"status": "blocked", **eligibility}
            result = await ToolTask._run_quest_request(
                title, f"【用途：关系建设】\n{context}", next_occurrence,
            )
            if not isinstance(result, dict):
                raise RuntimeError("Quest 未返回实际登记结果")
            growth = await service.register_social_growth(result.get("created_tasks", []), context.strip())
            return {"conclusion": result.get("formatted_output", ""), "created_tasks": result.get("created_tasks", []), **growth}
        if purpose_type == PURPOSE_GENERAL:
            return await ToolTask.quest_agent(title, context, next_occurrence)

        result = await ToolTask._run_quest_request(
            title, f"【用途：{purpose_type}】\n{context}", next_occurrence,
        )
        if not isinstance(result, dict):
            raise RuntimeError("Quest 未返回本次任务登记结果")
        created_tasks = result.get("created_tasks", [])
        response = {
            "conclusion": result.get("formatted_output", ""),
            "created_tasks": created_tasks,
            ("social_registered" if purpose_type == PURPOSE_SOCIAL else "verification_registered"): bool(created_tasks),
            "compensation_applied": False,
        }
        if created_tasks:
            setattr(self, "_social_registered" if purpose_type == PURPOSE_SOCIAL else "_verification_registered", True)
            titles = "、".join(str(task.get("title") or task["task_id"]) for task in created_tasks)
            response.update(await self._compensate_once(
                f"安排{purpose_type}", f"Quest 已登记{purpose_type}任务：{titles}。行动内容：{context.strip()}",
                purpose_type,
            ))
        return response

    async def _build_finalize_preview(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        """仅提示实际低值与验证安排；不把缺少验证作为退出门禁。"""
        from brain.amy.emotional_interface import get_emotional_interface
        from brain.amy.decision_feelings import AXIS_NAMES
        from config.truck_settings import get_truck_config

        ei = await get_emotional_interface()
        state = ei.feedback_state.snapshot()
        description = await ei.get_reflection_description()
        planned_verification = plan["enter_action"] and plan["purpose_type"] == PURPOSE_SELF_VERIFICATION
        planned_social = plan["enter_action"] and plan["purpose_type"] == PURPOSE_SOCIAL
        lines = ["这是第一次结束预览，尚未关闭反思。", description]
        if self._verification_registered:
            lines.append("本轮已经成功安排自修正验证；安排验证不代表验证已经成功。")
        elif planned_verification:
            lines.append("二次确认后将安排 PFCA 立即验证。本次仅暂存意图，不发布接力、不补偿。")
        else:
            lines.append("本轮尚未安排自修正验证。只整理经验后退出不会获得效能感的验证行动补偿。")
        if self._social_registered:
            lines.append("本轮已经成功安排主动社交；安排联系不代表已经得到他人的回应或认可。")
        elif planned_social:
            lines.append("二次确认后将安排 PFCA 主动社交。本次仅暂存意图，不发布接力、不补偿归属感。")
        else:
            lines.append("本轮尚未安排主动社交。只整理经验后退出不会获得归属感的行动补偿。")

        low_values = []
        low_axes = []
        for axis, key, truck_id in (
            ("competence", "competence_value", "reflection_efficacy_truck"),
            ("recognition", "value", "reflection_belonging_truck"),
        ):
            config = get_truck_config(truck_id)
            if config.ENABLED and state[key] < config.LOW_THRESHOLD:
                low_values.append(f"{AXIS_NAMES[axis]} {state[key]:g}/100（低于 {config.LOW_THRESHOLD:g}）")
                low_axes.append(axis)
        if low_values:
            lines.append("当前仍满足已启用的低值触发条件：" + "、".join(low_values) + "。")
            if "competence" in low_axes and not self._verification_registered and not planned_verification:
                lines.append("效能感仍低且尚未安排自修正验证，只整理经验不会获得效能感补偿。")
            if "recognition" in low_axes and not self._social_registered and not planned_social:
                lines.append("归属感仍低且尚未安排主动社交，只整理经验不会获得归属感补偿。")
            lines.append("如果退出时对应低值条件仍满足，系统恢复空闲后会再次启动反思，可能重复空转。自修正验证补偿效能感，不直接恢复归属感；主动社交补偿归属感，不直接恢复效能感。")
        else:
            lines.append("当前未满足已启用的情感低值触发条件。")
        lines.append("你可以先安排立即或择时行动（自修正验证 / 主动社交），也可以再次调用 finalize_reflection 确认结束。第二次将关闭，不会因低值或缺少行动再次要求确认。")
        return {"confirmation_required": True, "message": "\n".join(lines),
                "current_emotion": description, "pending_action": dict(plan)}

    async def finalize_reflection(self, enter_action: bool | None = None,
                                  action_purpose: str | None = None,
                                  purpose_type: str | None = None) -> Any:
        """两次收尾：首次预览并缓存意图，第二次执行安排、补偿与关闭。

        首次省略参数等价于普通退出；第二次省略参数沿用首次意图，显式参数可修改。
        合法的第二次调用不会因低值或未验证被拦截，不增加第三次确认。

        注意：笔记归档/创建已从收尾中拆出（archive_notes 工具由 LLM 自主调用），
        本工具不再自动整理笔记、不再写短期记忆总结；需要归档旧笔记请在调用本工具
        前先调 archive_notes。
        """
        previous = self._finalize_pending or {
            "enter_action": False, "action_purpose": "", "purpose_type": PURPOSE_GENERAL,
        }
        cancel_action = enter_action is False and purpose_type is None
        enter_action = previous["enter_action"] if enter_action is None else enter_action
        action_purpose = previous["action_purpose"] if action_purpose is None else action_purpose
        purpose_type = (PURPOSE_GENERAL if cancel_action else
                        previous["purpose_type"] if purpose_type is None else purpose_type)
        self._validate_purpose(purpose_type)
        if not isinstance(enter_action, bool):
            raise ValueError("enter_action 必须为布尔值")
        if purpose_type == PURPOSE_SOCIAL_GROWTH:
            raise ValueError("关系建设请通过 quest_agent 择时登记，不在立即行动入口结算")
        if purpose_type != PURPOSE_GENERAL and not enter_action:
            raise ValueError(f"{purpose_type}必须设置 enter_action=true")
        # 参数校验前置（在任何副作用之前）：enter_action=true 时 action_purpose
        # 必填，缺失属参数错误，抛 ValueError 交 LLM 自纠（_safe_execute 可恢复
        # 错误通道）。
        if enter_action:
            if not isinstance(action_purpose, str) or not action_purpose.strip():
                raise ValueError("enter_action=true 时必须提供 action_purpose")

        plan = {"enter_action": enter_action, "action_purpose": action_purpose,
                "purpose_type": purpose_type}
        if self._finalize_pending is None:
            preview = await self._build_finalize_preview(plan)
            self._finalize_pending = plan
            logger.info("finalize_reflection 第一次调用，仅预览并暂存意图")
            return preview

        from brain.pfc.reflection.reflection_core import get_reflection_instance
        reflection_core = get_reflection_instance()
        compensation = None

        # 可选接力：反思结束后进入行动状态（由 core.py run_pfc_cycle 消费）
        if enter_action:
            purpose = (action_purpose or "").strip()
            focus_state = get_global_session_state()
            previous_handoff = focus_state._data.get("pfc_handoff")
            focus_state._data["pfc_handoff"] = {
                "state": "action",
                "purpose": purpose,
            }
            if purpose_type != PURPOSE_GENERAL:
                try:
                    compensation = await self._compensate_once(
                        f"安排立即{purpose_type}", f"已登记 PFCA {purpose_type}行动接力：{purpose}",
                        purpose_type,
                    )
                except BaseException:
                    # 补偿保存失败时保留本轮反思；不留下未完成的接力请求。
                    if previous_handoff is None:
                        focus_state._data.pop("pfc_handoff", None)
                    else:
                        focus_state._data["pfc_handoff"] = previous_handoff
                    raise
                setattr(self, "_social_registered" if purpose_type == PURPOSE_SOCIAL else "_verification_registered", True)
            logger.info("finalize_reflection 已登记行动接力，反思结束后进入行动状态")

        await reflection_core.stop(exit_reason="reflection_done")
        logger.info("反思收尾完成: 退出标志已设置")
        if compensation is not None:
            return {"message": f"反思已结束，已安排 PFCA 立即{purpose_type}。", **compensation}
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
