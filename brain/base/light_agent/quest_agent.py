# -*- coding: utf-8 -*-
"""QuestAgent - 长期任务（Quest）管理Agent

设计要点（2026-09-17 由 task_register_agent 任务登记重做而来）：
- 定位：三权分立中管 WHAT / WHETHER——负责任务的阶段拆解（滚动式）、
  进展追踪（当前到哪一步）、推进裁决（系统当前是否有能力推进下一阶段）。
  唯独不负责具体策略执行（HOW 归反思系统，现场执行归 PFCA）。
- 滚动式阶段规划：社交任务千变万化，无法在创建时一次性完整拆解全部阶段。
  阶段拆解与进展裁决的结论以任务记录的独立字段承载（brief 简要总结 /
  conclusion 汇报结论 / stage 当前阶段 / notes 任务笔记），
  随任务推进在每轮审视中修订。推进即整理：quest 判定可推进时，调用
  update_task_status 重写 stage 与 conclusion，并完全清空旧 notes、
  填入一条简洁的新笔记（quest 独占整理权）。
  计时拆为两个时间：next_occurrence（下次处理时间，唤起频率）与
  deadline（最终汇报时间，硬截止；到点未达标则出未达标说明收尾）。
- 推进裁决：裁决依据是信息 / 材料 / 时机是否就绪。核实手段是通用社交查询
  工具（query_history / query_address_book，只读、不推进任何游标），
  由本 agent 自主查证聊天记录与通讯录，而非只信 backlog 的转述。
- 交互方式对齐 inner_persona_agent：对外作为工具被调用（PFCA / proactive），
  一次调用返回裁决性结论；内部保留多轮循环（每轮刷新任务全景，自主规划）。
- 多轮执行：每轮先拉取"全景"（进行中任务），让 agent 自己规划，再调工具。
  本轮可多次 create / update；当所有 backlog 已处理、任务表已整理得当时，
  直接以自然语言回复即视为结束本轮。
- 全异步：复用既有 TaskOperations（已经是 async），本文件不再引入同步 IO。

任务工具（create_task / update_task_status / query_task_status）的 schema 与实现
收口至 ToolTask（common_ways/tool_task.py）；社交查询工具（query_history /
query_address_book）收口至 ToolSocial（common_ways/tool_social.py）；
记事本查询工具（list_notes / query_note）收口至 ToolHC（common_ways/tool_hc.py），
本类仅做委托拼装。任务信息注入时（首条 user 消息）随附【记事本手册】
（notepad.render_manual 章节式概览），细节经 query_note 溯源（2026-09-19）。
"""

import json
from typing import List, Dict, Any, Optional

from brain.base.light_agent.lightweight_agent import LightweightAgent
from brain.base.light_agent.quest_history_manager import get_quest_history_manager
from brain.base.common_ways.tool_task import ToolTask
from brain.base.common_ways.tool_social import ToolSocial
from brain.base.common_ways.tool_hc import ToolHC
from utils.logger import get_logger
from utils.llm_client_manager import extract_reasoning
from utils.error_handling import async_handle_errors
from config.role_settings import role_config
from brain.hc.database.task import TaskOperations

logger = get_logger(__name__, "DEBUG")


# 单次 run() 最多允许多少轮 LLM 决策（防止死循环把 token 烧光）
MAX_TURNS = 8

# 操作类型（与 ToolPersistent.quest_agent schema 的 enum 对齐）
OP_CREATE_TASK = "create_task"
OP_UPDATE_TASK = "update_task"
OP_REVIEW_STAGE = "review_stage"

_OPERATION_TYPE_NAMES = {
    OP_CREATE_TASK: "create_task（新建任务并拆解当前能确认的阶段）",
    OP_UPDATE_TASK: "update_task（推进/修订已有任务）",
    OP_REVIEW_STAGE: "review_stage（阶段裁决：进展到哪一步、下一阶段能否推进）",
}


class QuestAgent(LightweightAgent):
    """长期任务（Quest）管理Agent - 阶段拆解 / 进展追踪 / 推进裁决。

    工具权限分区（2026-09-17）：
    - 裁决级（本类私有，不作为 common way）：create_task / update_task_status
      —— 创建任务、记录重写/阶段推进、完结，全系统仅 quest 可调；
    - 记录级（ToolTask 通用层）：append_task_log —— 其他 agent 可给任务
      添加一条笔记（追加 notes 末尾，待 quest 整理熔合）；
    - 查询级（ToolTask 通用层 + ToolSocial）：query_task_status / query_history /
      query_address_book —— 全员只读。
    """

    # ===================== 裁决级工具（私有） =====================
    @classmethod
    def get_create_task_tool_schema(cls) -> Dict[str, Any]:
        """返回 create_task 工具的 schema（裁决级，私有于本类）。"""
        return {
            "type": "function",
            "function": {
                "name": "create_task",
                "description": (
                    "创建一个需要下次唤醒才能完成的长期任务。\n"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "任务标题，简短概括任务",
                        },
                        "brief": {
                            "type": "string",
                            "description": (
                                "简要总结（不超过 50 字）：说明这条任务的前因与目的"
                                "——从哪来、要达成什么。必须笼统、禁流水账。"
                            ),
                        },
                        "stage": {
                            "type": "string",
                            "description": (
                                "当前阶段（一句话，简洁的刚性目标）：这条任务此刻还需"
                                "要做什么。不写流水账、不写策略方案。"
                            ),
                        },
                        "next_occurrence": {
                            "type": "string",
                            "description": (
                                "下次处理时间（唤起频率，想催紧就排近）："
                                "ISO 8601（如 2024-01-20T10:00:00），或「尽快」表示现在执行。"
                            ),
                        },
                        "deadline": {
                            "type": "string",
                            "description": (
                                "最终汇报时间（ISO 8601 硬截止）：到点仍未达标，"
                                "就必须出「未达标说明 + 已有材料」上报收尾。必填。"
                            ),
                        },
                    },
                    "required": ["title", "brief", "stage", "next_occurrence", "deadline"],
                },
            },
        }

    @classmethod
    def get_update_task_status_tool_schema(cls) -> Dict[str, Any]:
        """返回 update_task_status 工具的 schema（裁决级，私有于本类）。"""
        return {
            "type": "function",
            "function": {
                "name": "update_task_status",
                "description": (
                    "推进 / 修订 / 完结一个已有任务（由你裁决）。\n"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_title": {
                            "type": "string",
                            "description": "要修改的任务标题，用于定位任务",
                        },
                        "conclusion": {
                            "type": "string",
                            "description": (
                                "当前已获取、用于汇报的结论（不超过 100 字）："
                                "只写结论性材料，不写过程与策略。没有则填空串。"
                            ),
                        },
                        "stage": {
                            "type": "string",
                            "description": (
                                "推进后的当前阶段（一句话，简洁的刚性目标）："
                                "更新为最新的「还需要做什么」。"
                            ),
                        },
                        "notes": {
                            "type": "string",
                            "description": (
                                "一条简洁的新任务笔记："
                                "你独占整理权——本次调用会完全清空旧笔记，"
                                "你必须把现有信息熔成这一条简洁笔记填入，"
                                "不保留零散旧条目、不堆砌过程细节。"
                            ),
                        },
                        "brief": {
                            "type": "string",
                            "description": (
                                "简要总结（不超过 50 字，前因与目的）。"
                                "仅当需要修正时传入；留空或省略表示保持不变。"
                            ),
                        },
                        "next_occurrence": {
                            "type": "string",
                            "description": (
                                "下次处理时间（唤起频率，想催紧就排近）："
                                "ISO 8601（如 2024-01-20T10:00:00）、「尽快」（现在执行）、"
                                "或「完结」（标记任务已完成，不再设下次时间）。"
                            ),
                        },
                        "deadline": {
                            "type": "string",
                            "description": (
                                "最终汇报时间（ISO 8601）。仅当需要调整硬截止时传入；"
                                "留空或省略表示保持不变。"
                            ),
                        },
                    },
                    "required": ["task_title", "conclusion", "stage", "notes",
                                 "next_occurrence"],
                },
            },
        }

    # ===================== 工具表拼装 =====================
    def _initialize_tool_functions(self) -> Dict[str, Any]:
        # 裁决级（私有）+ 通用级（ToolTask：append_task_log / query_task_status）
        functions = dict(ToolTask.get_tool_functions())
        functions["create_task"] = QuestAgent._create_task_impl
        functions["update_task_status"] = QuestAgent._update_task_status_impl
        # query_history 不传 consumer_id（默认 None）＝纯只读、不推进任何游标：
        # quest agent 只是核实事实，读历史不得影响 truck 的未读检测
        functions["query_address_book"] = ToolSocial.query_address_book
        functions["query_history"] = ToolSocial.query_history
        # 记事本查询（对齐 reflection，2026-09-19）：list_notes / query_note
        # —— 任务全景注入附带【记事本手册】，细节经 query_note 溯源
        functions["list_notes"] = ToolHC.list_notes
        functions["query_note"] = ToolHC.query_note
        return functions

    def __init__(self):
        super().__init__()
        # 跨 run() 持久化的对话状态。
        # 设计：在一个事件链（一个 PFCA 会话）中，PFCA 可能会多次呼叫本 agent，
        # 每次调用都应该延续上一次的上下文（messages / backlog / tools_called），
        # 保证两个 agent 之间的多轮通讯是连续的，而不是每次都从零开始。
        # 调用方需要在事件链结束时显式调用 reset() 清空。
        self._messages: List[Dict[str, Any]] = []
        self._tools_called: List[Dict[str, Any]] = []
        self._last_backlog_text: str = ""
        self._final_text: str = ""
        self._is_initialized: bool = False

    def reset(self) -> None:
        """清空跨 run() 的对话状态，并归档终结事件链上下文的写入资格。"""
        self._messages = []
        self._tools_called = []
        self._last_backlog_text = ""
        self._final_text = ""
        self._is_initialized = False
        get_quest_history_manager().archive_context()
        logger.debug("[quest] 上下文已 reset")

    def _push(self, message: Dict[str, Any]) -> None:
        """追加一条消息：内存 LLM 上下文（self._messages）+ 事件链上下文
        管理器双写（链内落盘到 event_chains/{chain_id}/quest_{HHMMSS}.json）。"""
        self._messages.append(message)
        get_quest_history_manager().append(dict(message))

    def get_available_tools(self) -> List[Dict[str, Any]]:
        return (
            # 裁决级（私有）：create_task / update_task_status
            [
                QuestAgent.get_create_task_tool_schema(),
                QuestAgent.get_update_task_status_tool_schema(),
            ]
            # 通用级（ToolTask）：append_task_log / query_task_status
            + ToolTask.get_task_tool_schemas()
            # 查询级（ToolSocial）：query_history / query_address_book
            + ToolSocial.get_query_tool_schemas()
            # 记事本查询（对齐 reflection，2026-09-19）：
            # list_notes / query_note —— 配合注入的【记事本手册】溯源细节
            + [
                ToolHC.get_list_notes_tool_schema(),
                ToolHC.get_query_note_tool_schema(),
            ]
        )

    def get_system_prompt(self) -> str:
        return f"""
【系统定位】
你是复杂类人agent系统的一环，系统的目标是完成社交任务、或通过社交达成目的。
你负责管理系统的任务与目标，负责审批以及验证是否可以进入下一个阶段，
你不负责具体怎么做。你只需要关心现在的信息。
你对任务的管理有严格要求，你需要独立判断是否要创建任务，而不是盲从输入的信息：
慎重对待交付给你的信息，结合信息本身与当前系统状态，判断是否遵从并创建任务。
你的职责范围是：任务登记、推进审批（资源与信息是否就绪）、向调用方输出结论。

【职责一：任务登记】
是否登记不能从单一维度判断，要多维度共同考虑：
- 像与他人问好这种简单的事，也可能因为自身缺乏相关经验或存在缺陷，
  算作真正需要认真对待的任务去处理。
- 反过来，某些极其复杂的任务也可能因为经验充足，不需要登记即可凭既有经验直接完成。
你根据其他agent交付给你的信息，判断任务的性质，是否值得登记与创建。
登记结论必须说明理由，且理由要落在价值上：这个任务能给系统带来什么——
完善自身能力、补上经验盲区、提高与主人的关系、提升社交水平、
了解大家的状态等；同时说明自己当前的资源状况（有没有同类经验、缺什么）。

【职责二：推进审批（资源管理分配）】
你不负责编纂具体策略，也不需要知道具体做什么，你只需要审批：
以现在的信息看，系统有没有能力做好下一步——
是否有足够的资源、收集了足够的信息、时机是否合适。
判断依据：任务记录里的内容与其他agent交付给你的信息。你不自行查证、不推测不脑补。
可以 → 批准推进，调用 update_task_status 更新任务记录；不行 → 打回，明确说缺的是信息、材料还是时机。

执行任务存在多种风险，你需要时刻记在心里：
1 失败的代价不可承受——哪怕是最微小的一句话错误。
2 对话是及时的、瞬息万变的，策略和话题会有颠覆性的改变。
3 任务通常涉及多个角色甚至个体（单对多），需要一个人同时管理多场对话。
4 任务本身复杂：往往既夹杂社交共情安抚，又夹杂真实想法，
  且任务背后的真实目的通常不会被完全告知。
5 不同社交社群之间存在大量社交黑话，需要大量时间学习。

【职责三：输出】
输出只包含结论与依据，别无其他：先给结论，再给理由。
- 登记：登记 / 不登记 + 理由（价值与事实）；
- 推进：可推进 / 暂缓 / 受阻 + 依据；受阻时说明缺什么（信息、材料还是时机）。
若你驳回了对方的建议，必须说明驳回理由。
禁止输出建议、方案、策略、计划（如"下次和主人聊天时可以……"、"建议先……"）——
那些是其他系统的职责，越权的输出比没有输出更糟。

【任务记录字段】
任务记录由四个独立字段承载，各司其职，不再靠文本约定拼接：
- brief（简要总结，≤50 字）：这条任务的前因与目的——从哪来、要达成什么；
- conclusion（已获取的汇报结论，≤100 字）：当前已拿到、可直接用于汇报的结论；
- stage（当前阶段）：还需要做什么，一句话刚性目标；
- notes（任务笔记）：其他agent通过 append_task_log 往这里追加新笔记，
  这是他们的低权限记录通道，你不用管他们写得整不整齐；你独占整理权。

【推进即整理】
一旦你判定该任务可以推进进度，就必须调用 update_task_status，且必须：
1. 更新 stage 为推进后的最新阶段（还需要做什么）；
2. 更新 conclusion 为本轮已获取、可用于汇报的结论；
3. 填入一条简洁的新 notes：本次调用会完全清空旧笔记，你必须把现有信息
   熔成这一条简洁笔记，不保留零散旧条目、不堆砌过程细节。
整理后的笼统呈现比原始堆砌更重要。

【两个时间】
- next_occurrence（下次处理时间）：唤起频率，你每次被咨询时写死——想催紧就排近、
  想缓就排远；它只决定任务卡车何时再唤醒 PFCA 来推进该任务。
- deadline（最终汇报时间）：硬截止；创建任务时必填。到点后无论是否达标都要收尾。
填法：next_occurrence 用 ISO 8601 具体时间，或「尽快」（现在就执行，等效设为当前时间，
任务卡车下一轮检测即视为到期触发；若触发时当前正有其他环节在处理，触发请求会排队，
在本环节结束后立即执行）。不用纠结到分钟，时间语义由系统统一解析。

【达标与收尾】
- 未达标：继续设 next_occurrence（可逐轮收紧逼近 deadline），让 PFCA 继续推进；
- 已达标：更新记录后，next_occurrence 填「完结」——不再设下次时间，任务自动停止触发；
- 到最终汇报时间仍未达标：不再设下次时间（next_occurrence 填「完结」），
  把「未达标说明 + 已有材料」写入 conclusion 作为最终上报，收尾，不再无限触发。

【给 PFCA 的两条信息】
- 登记回执：你完成登记（create_task）后，在输出结论里附上如下回执（先结论后回执）：
  「已登记，现处<当前阶段>，未到可汇报地步。你要在<最终汇报时间>前调查到足够信息，
   交付我登记；我审查后才能推进到下一阶段。」
- 定时唤醒：由任务卡车在到期时自动发给 PFCA（你不必生成）：任务/当前阶段/
  距最终汇报时间还剩 X/要求推进并汇报，直到你判定达标。

【记事本手册】
每次审视时，任务全景后附带【记事本手册】——记事本中全部已落库活动笔记的
章节式概览（长期积累的经验与认知底色，如与主人的相处尺度、话术教训等），
可作为推进裁决时「资源/信息是否就绪」的判断参考。
需要某一章节的完整细节时，用 list_notes / query_note 工具按标题查询，
禁止脑补手册中未出现的内容。
"""

    # ===================== 五段式骨架（run 自实现多轮循环，其余给默认实现） =====================
    def _preprocess(self, query: str) -> Any:
        return query

    def _should_continue(self, response_message, results: List[Dict[str, Any]]) -> bool:
        return bool(response_message.tool_calls)

    def _postprocess(self, content: str, extra_info: Any) -> Dict[str, Any]:
        return {"content": content, "extra_info": extra_info}

    async def _call_llm(self, messages: list, *, level: str = "flash",
                        tool_choice: Optional[str] = None,
                        system_prompt: Optional[str] = None, **kwargs):
        """统一 LLM 调用：走 call_llm。

        Quest 自建循环（不走父类 ``_llm_loop``），故在此单点声明 agent 身份，
        保证 token 统计归到 QuestAgent 而非外层会话。
        """
        from utils.llm_client_manager import call_llm, extract_reasoning, agent_scope

        sp = system_prompt if system_prompt is not None else self.get_system_prompt()
        full_messages = [{"role": "system", "content": sp}] + messages
        with agent_scope(self.agent_name or type(self).__name__):
            return await call_llm(
                level=level,
                messages=full_messages,
                tools=self.get_available_tools(),
            )

    # ===================== 工具实现 =====================
    # 裁决级（私有，2026-09-17 由 ToolTask 私有化回本类——通用层不应持有裁决级写权限）：

    @staticmethod
    def _resolve_next_occurrence(next_occurrence: Optional[str]) -> str:
        """解析 next_occurrence 的「尽快」快捷语义（2026-09-19）。

        「尽快」＝现在就执行：本质是把时间设置为当前时间——任务卡车（task_truck）
        下一轮周期检测即把它视为已到期并触发 PFCA；若触发时 PFCA 正忙于其他环节，
        触发请求会在 station 队列排队，本环节结束后立即执行（即"本阶段任务结束后
        立即执行"）。其余输入（ISO 8601 时间 / 空串）原样返回。
        """
        if next_occurrence and "尽快" in next_occurrence:
            from datetime import datetime
            return datetime.now().isoformat(timespec="seconds")
        return next_occurrence or ""

    @staticmethod
    async def _create_task_impl(
        title: str,
        brief: str,
        stage: str,
        next_occurrence: Optional[str] = None,
        deadline: Optional[str] = None,
    ) -> Dict[str, Any]:
        """创建一个新任务。

        brief 为简要总结（前因与目的，≤50 字）；stage 为当前阶段（还需要做什么）。
        next_occurrence 支持「尽快」（解析为当前时间，立即到期触发）；
        deadline 为最终汇报时间（硬截止）。

        Returns:
            {"task_id": str, "title": str, "brief": str, "stage": str,
             "next_occurrence": str, "deadline": str}
        """
        from brain.hc.database.task import TaskOperations

        next_occurrence = QuestAgent._resolve_next_occurrence(next_occurrence)
        deadline_val = deadline or ""

        logger.info(
            f"创建任务: title={title}, next_occurrence={next_occurrence}, "
            f"deadline={deadline_val}"
        )

        task_ops = TaskOperations()
        task_id = await task_ops.create_task(
            title=title,
            brief=brief,
            stage=stage,
            next_occurrence=next_occurrence,
            deadline=deadline_val,
        )

        logger.info(f"任务创建成功: task_id={task_id}")

        return {
            "task_id": task_id,
            "title": title,
            "brief": brief,
            "stage": stage,
            "next_occurrence": next_occurrence,
            "deadline": deadline_val,
        }

    @staticmethod
    async def _update_task_status_impl(
        task_title: str,
        conclusion: Optional[str] = None,
        stage: Optional[str] = None,
        notes: Optional[str] = None,
        next_occurrence: Optional[str] = None,
        brief: Optional[str] = None,
        deadline: Optional[str] = None,
    ) -> Dict[str, Any]:
        """推进 / 修订 / 完结任务：更新结论与阶段、整列覆盖笔记、更新两个时间。

        quest 的 schema 要求 conclusion / stage / notes 三字段必填，故 quest 推进时
        三者恒有值——notes 传入即整列覆盖旧笔记（完全清理旧零散条目，只留这一条新笔记）。
        其他调用方（如 proactive 的推迟唤醒）可省略不传的字段，省略（None）表示保持不变。
        next_occurrence 含「完结」时标记任务已完成（写 end_time），否则保持进行中
        并更新下次处理时间；支持「尽快」（解析为当前时间）。
        brief / deadline 省略（None）表示不变，传入空串表示清空。

        Returns:
            {"task_title": str, "conclusion": str, "stage": str, "notes": str,
             "next_occurrence": str, "deadline": Optional[str], "status": str,
             "success": bool}
        """
        from brain.hc.database.task import (
            TaskOperations, TASK_STATUS_ACTIVE, TASK_STATUS_COMPLETED,
        )

        logger.info(
            f"更新任务: task_title={task_title}, next_occurrence={next_occurrence}, "
            f"deadline={deadline}"
        )

        if next_occurrence and "完结" in next_occurrence:
            status = TASK_STATUS_COMPLETED
            next_occurrence_val = ""
        else:
            status = TASK_STATUS_ACTIVE
            next_occurrence_val = QuestAgent._resolve_next_occurrence(next_occurrence)

        kwargs: Dict[str, Any] = {
            "status": status,
            "next_occurrence": next_occurrence_val,
        }
        # 推进即整理：整列覆盖旧笔记（完全清理旧零散条目，只留这一条新笔记）
        if conclusion is not None:
            kwargs["conclusion"] = conclusion
        if stage is not None:
            kwargs["stage"] = stage
        if notes is not None:
            kwargs["notes"] = notes.strip()
        if brief is not None:
            kwargs["brief"] = brief
        if deadline is not None:
            kwargs["deadline"] = deadline

        task_ops = TaskOperations()
        success = await task_ops.update_task(task_title, **kwargs)

        if success:
            logger.info(f"任务更新成功: {task_title} (status={status})")
        else:
            logger.warning(f"任务更新失败，可能找不到: {task_title}")

        return {
            "task_title": task_title,
            "conclusion": conclusion,
            "stage": stage,
            "notes": notes,
            "next_occurrence": next_occurrence_val,
            "deadline": deadline,
            "status": status,
            "success": success,
        }

    # 记录级 / 查询级已收口至 ToolTask（append_task_log / query_task_status）
    # 与 ToolSocial（query_history / query_address_book），
    # 经 _initialize_tool_functions 分发表直接拼装，本类不再重复实现

    # ===================== 多轮入口 =====================
    @async_handle_errors()
    async def run(
        self,
        additional_content: str = None,
        operation_type: str = None,
    ) -> Dict[str, Any]:
        """多轮执行：每轮 LLM 决策 → 工具执行 → 刷新任务全景，
        直到 LLM 直接给文字回复（视为结束）或达到 MAX_TURNS。"""
        backlog_lines = []
        if additional_content:
            backlog_lines.append(f"待处理内容：{additional_content}")
        if operation_type:
            op_name = _OPERATION_TYPE_NAMES.get(
                operation_type, operation_type
            )
            backlog_lines.append(f"操作类型：{op_name}")
        if not backlog_lines:
            backlog_lines.append(
                "（无显式 backlog，请主动审视当前任务表：核对各任务阶段进展与推进裁决）"
            )
        backlog_text = "\n".join(backlog_lines)

        system_prompt = self.get_system_prompt()

        # 事件链上下文接入（2026-09-18）：新链首次进入时，先终结上一链可能残留的
        # 写入资格（station 链尾强制归档若因崩溃未执行），再把 system prompt 作为
        # 链文件首条落盘（提示词文档随链持久化）。同一链内多次 run() 时
        # current_chain_id 与当前链一致，跳过——消息延续累积到同一落盘文件。
        from utils.chain_context import get_current_chain_id
        history_mgr = get_quest_history_manager()
        chain_id = get_current_chain_id()
        if chain_id and history_mgr.current_chain_id != chain_id:
            history_mgr.archive_context()
            history_mgr.append({"role": "system", "content": system_prompt})

        if not self._is_initialized:
            initial_active = await self._fetch_active_tasks_text()
            initial_manual = await self._fetch_notebook_manual_text()
            initial_user = self._build_user_message(
                backlog_text,
                turn_index=0,
                active_text=initial_active,
                notebook_manual_text=initial_manual,
                continuation_note=None,
            )
            self._push({"role": "user", "content": initial_user})
            self._is_initialized = True
        else:
            # 续接 run()：完整全景已在首轮注入过，这里只给紧凑简报
            # （防大段记录反复注入稀释注意力；细节可经 query_task_status 溯源）
            refreshed_active = await self._fetch_active_tasks_brief()
            refreshed_manual = await self._fetch_notebook_manual_text()
            continuation_note = (
                "PFCA 又发来一次请求，请基于你之前的整理结果继续处理。\n"
                "下面的 backlog 是本次新增/修订的内容。"
            )
            self._push({
                "role": "user",
                "content": self._build_user_message(
                    backlog_text,
                    turn_index=0,
                    active_text=refreshed_active,
                    notebook_manual_text=refreshed_manual,
                    continuation_note=continuation_note,
                    active_is_brief=True,
                ),
            })

        self._last_backlog_text = backlog_text

        finished = False
        last_assistant_text: str = self._final_text or ""

        for turn in range(1, MAX_TURNS + 1):
            logger.info(f"[quest] turn {turn}/{MAX_TURNS}")

            response = await self._call_llm(
                self._messages,
                level='flash',
                tool_choice="auto",
                system_prompt=system_prompt,
            )
            response_message = response.choices[0].message
            last_assistant_text = response_message.content or ""

            # 1) 没有 tool_calls：LLM 直接给文字回复 → 视为本轮收尾
            if not response_message.tool_calls:
                self._push({
                    "role": "assistant",
                    "content": last_assistant_text,
                })
                finished = True
                self._final_text = last_assistant_text
                logger.info(
                    f"[quest] turn {turn} LLM 直接文字回复，结束本轮"
                )
                break

            # 2) 把 assistant 消息原样塞回 messages（带 tool_calls）
            self._push(self._serialize_assistant_message(response_message))

            # 3) 逐个执行工具，把结果以 role=tool 灌回
            for tool_call in response_message.tool_calls:
                function_name = tool_call.function.name
                try:
                    function_args = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError:
                    function_args = {}

                logger.info(
                    f"[quest] turn {turn} tool={function_name} args={function_args}"
                )

                if function_name not in self.tool_functions:
                    tool_result = {"error": f"未知工具: {function_name}"}
                else:
                    try:
                        tool_result = await self.tool_functions[function_name](**function_args)
                    except Exception as e:
                        logger.exception(f"[quest] 工具 {function_name} 执行异常")
                        tool_result = {"error": f"{type(e).__name__}: {e}"}

                self._tools_called.append({"tool": function_name, "args": function_args, "result": tool_result})

                self._push({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(tool_result, ensure_ascii=False, default=str),
                })

            # 4) 全程仅首条 user 注入，后续轮次不再塞 user 消息（2026-09-20）：
            #    上一轮「全貌刷新」的 user 注入会反复把完整上下文喂给 LLM，
            #    既膨胀上下文又干扰其表现。后续轮次只靠 tool 结果自然推进，
            #    任务最新状态由 update_task_status / query_task_status 的返回携带。

        if not finished:
            logger.warning(
                f"[quest] 达到 MAX_TURNS={MAX_TURNS} 仍未自然结束，强制收尾"
            )

        return {
            "status": "finished" if finished else "max_turns_reached",
            "turns": turn if not finished else turn,
            "tools_called": list(self._tools_called),
            "formatted_output": self._final_text if finished else last_assistant_text,
        }

    # ===================== 构造 user 消息 =====================
    def _build_user_message(
        self,
        backlog_text: str,
        turn_index: int,
        active_text: str,
        notebook_manual_text: str = "",
        continuation_note: Optional[str] = None,
        active_is_brief: bool = False,
    ) -> str:
        """每轮 user 消息：backlog + 当前任务全景 + 记事本手册 + 轮次提示。

        注入时机（2026-09-20 起全程仅首条 user 注入）：
        - 首次 run()：完整全景 + 手册（turn_index==0）；
        - 续接 run()（PFCA 再次请求）：compact 简表 + 刷新后的手册
          （turn_index==0 且 active_is_brief=True）；
        - 多轮循环内不再注入任何 user 消息——后续轮次只靠 tool 结果
          自然推进，任务最新状态由工具返回携带。

        记事本手册（render_manual 章节式裸文本，自带【记事本手册】段头）仅在
        每轮 run() 的首条消息注入一次（turn_index == 0，初始与续接两条路径
        都会重新拉取）——长期底色先于思考，细节经 query_note 工具溯源。

        2026-10-01 改造：移除「最近完结的任务列表」注入（RECENT_COMPLETED_LIMIT）。

        防上下文膨胀（2026-09-20）：
        - turn_index > 0 的多轮提示不再重复整段 backlog 原文
          （backlog 未变，此前已注入过；仅保留进度提示与剩余指令）；
        - active_is_brief=True 时用紧凑简报替代完整任务全景
          （续接 run() 用，细节由 query_task_status 按需查询）。
        """
        from datetime import datetime
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if continuation_note:
            head = f"{continuation_note}\n\n【本次 backlog】\n{backlog_text}"
        elif turn_index == 0:
            head = "【本轮 backlog】\n" + backlog_text
        else:
            head = (
                "【本轮 backlog（未变，见前文）】\n\n"
                f"你已经执行了 {turn_index} 轮工具调用。"
                "请继续：处理剩余 backlog / 核对各任务阶段进展与推进裁决，"
                "完成后直接以文字回复即可结束本轮。"
            )

        if active_is_brief:
            parts = [head, f"【当前任务全景·简表（进行中）】\n{active_text}"]
        else:
            parts = [head, f"【当前任务全景（进行中）】\n{active_text}"]

        # 记事本手册仅首条注入（长期底色先于当前时间）
        # 续接 run()（active_is_brief=True 但 turn_index==0）也注入——
        # 手册体积受 render_manual 限幅（约 300 字），刷新后的手册值得重投
        if turn_index == 0 and notebook_manual_text:
            parts.append(notebook_manual_text)

        parts.append(f"【当前时间】{current_time}")

        return "\n\n".join(parts)

    async def _fetch_notebook_manual_text(self) -> str:
        """异步拉取记事本手册（章节式裸文本，自带段头）。

        手册为空（无活动笔记 / 全部被限幅挤出）时返回空串，调用方跳过该段
        （语义对齐 reflection 的 _collect_notebook_manual：空库不注入）。
        拉取失败直接上抛崩溃阻断（硬约定，不吞错）。"""
        from brain.hc.shortlight import get_shortlight_notepad

        return await get_shortlight_notepad().render_manual()

    async def _fetch_active_tasks_text(self) -> str:
        """异步拉取当前所有进行中的任务，格式化为字符串。

        拉取失败直接上抛崩溃阻断（硬约定），不吞错。"""
        task_ops = TaskOperations()
        ids = await task_ops.get_active_tasks()
        if not ids:
            return "（当前没有进行中的任务）"
        lines = []
        for i, tid in enumerate(ids[:20], 1):
            formatted = await task_ops.format_task_by_id(tid)
            if not formatted.startswith("未找到"):
                lines.append(f"{i}. {formatted}")
        return "\n".join(lines) if lines else "（当前没有进行中的任务）"

    async def _fetch_active_tasks_brief(self) -> str:
        """异步拉取当前所有进行中的任务，格式化为每任务一行的紧凑简报。

        供多轮循环后续轮次 / 续接 run() 使用：每行只含「任务标题 + 当前阶段 +
        状态」，避免把每个任务的完整记录全文反复注入上下文稀释 LLM 注意力；
        需要全文细节时由 query_task_status 按需查询。

        拉取失败直接上抛崩溃阻断（硬约定），不吞错。"""
        task_ops = TaskOperations()
        ids = await task_ops.get_active_tasks()
        if not ids:
            return "（当前没有进行中的任务）"
        lines = []
        for i, tid in enumerate(ids[:20], 1):
            formatted = await task_ops.format_task_by_id(tid)
            if formatted.startswith("未找到"):
                continue
            brief = self._brief_from_task_text(formatted)
            lines.append(f"{i}. {brief}")
        return "\n".join(lines) if lines else "（当前没有进行中的任务）"

    @staticmethod
    def _brief_from_task_text(formatted: str) -> str:
        """把 format_task_by_id 的完整文本压缩为单行简报。

        保留：标题、当前阶段、状态、时间；丢弃简要/结论/笔记正文。
        """
        lines = formatted.splitlines()
        title = next((ln.removeprefix("名称：") for ln in lines if ln.startswith("名称：")), "")
        stage = next((ln for ln in lines if ln.startswith("当前阶段：")), "")
        status = next((ln for ln in lines if ln.startswith("状态：")), "")
        time_part = next((ln for ln in lines if ln.startswith("时间：")), "")
        parts = [title]
        if stage:
            parts.append(stage)
        if status:
            parts.append(status)
        if time_part:
            parts.append(time_part)
        return "｜".join(parts)

    def _serialize_assistant_message(self, response_message) -> Dict[str, Any]:
        """把 LLM 返回的 assistant 消息序列化成可回灌 messages 的 dict。"""
        return {
            "role": "assistant",
            "content": response_message.content or "",
            "reasoning_content": extract_reasoning(response_message),
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in response_message.tool_calls
            ],
        }

    # ===================== 回调（保留空实现，兼容父类约定） =====================
    def _on_tool_result(
        self,
        tool_name: str,
        result: Any,
        results: Dict[str, Any],
    ) -> None:
        pass
