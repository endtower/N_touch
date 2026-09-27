# -*- coding: utf-8 -*-
"""通用工具：ToolTask（任务管理通用级工具：append_task_log / query_task_status）

任务系统收口模块（2026-09-15 由 tool_event.py 事件系统重做而来）。

2026-09-17 权限分区调整：create_task / update_task_status 属**裁决级**工具
（quest agent 独占：创建任务、概况重写/阶段推进、完结），已私有化回
QuestAgent 本体，不再作为 common way——通用层不应持有裁决级写权限。

本模块只收口**通用级**工具：
- append_task_log：向任务笔记末尾添加一条一级笔记（记录级，低权限：
  只添加，不改阶段、不完结；笔记的分级整理由 quest agent 重写概况时进行）
- query_task_status：查询任务状态——不传标题返回所有未完成任务列表；
  传入具体标题返回该任务细节
- quest_agent：联系 quest agent 的入口通道（2026-09-18 新增）——转交一段内容，
  由 quest agent 多轮裁决处理（审批推进 / 登记任务 / 修订任务）。本工具只负责
  转交，不做任何任务写操作：创建任务、概况重写、阶段推进、完结等裁决级写权限
  仍独占于 quest agent 本体，不违反上面的权限分区

阶段拆解与推进裁决（quest_agent 的 WHAT/WHETHER 职责）以任务概况文本 +
QuestAgent 上下文承载，不落结构化列、不进工具参数；核实任务实际进展
由 QuestAgent 通过 ToolSocial（query_history / query_address_book）只读查询。

全异步：底层走 TaskOperations（纯异步），本模块不引入同步 IO；
失败直接上抛崩溃阻断（硬约定），不包 async_handle_errors 吞错。
"""

from typing import Dict, Any, List, Callable, Optional

from utils.logger import get_logger

logger = get_logger('ToolTask', "DEBUG")


class ToolTask:
    """任务管理通用级工具：append_task_log + query_task_status + quest_agent（联系通道）。"""

    # ============ 工具 schema（提示词） ============

    @classmethod
    def get_query_task_status_tool_schema(cls) -> Dict[str, Any]:
        """返回 query_task_status 工具的 schema。"""
        return {
            "type": "function",
            "function": {
                "name": "query_task_status",
                "description": (
                    "查询当前的任务状态。\n"
                    "不传 task_title 时返回所有未完成任务的列表；"
                    "传入某一具体任务标题时返回该任务的细节。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_title": {
                            "type": "string",
                            "description": "要查询的任务标题。留空则查询所有未完成的任务",
                        },
                    },
                    "required": [],
                },
            },
        }

    @classmethod
    def get_append_task_log_tool_schema(cls) -> Dict[str, Any]:
        """返回 append_task_log 工具的 schema（2026-09-17 新增，2026-09-18 笔记化，记录级低权限）。"""
        return {
            "type": "function",
            "function": {
                "name": "append_task_log",
                "description": (
                    "给某个进行中的任务添加一条笔记"
                    "（如：学到/观察到的经验、与主人沟通的结果、拟定的草稿、下一步安排）。\n"
                    "笔记将作为一级条目追加到任务概况末尾，等待 quest agent 整理归位；"
                    "只添加笔记，不会改变任务阶段、不会完结任务。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_title": {
                            "type": "string",
                            "description": "要添加笔记的任务标题，用于定位任务",
                        },
                        "content": {
                            "type": "string",
                            "description": "要添加的一条笔记（一句话，具体、可核实）",
                        },
                    },
                    "required": ["task_title", "content"],
                },
            },
        }

    @classmethod
    def get_quest_agent_tool_schema(cls) -> Dict[str, Any]:
        """返回 quest_agent 工具的 schema（联系 quest agent 的入口通道，2026-09-18 新增）。

        不进 get_task_tool_schemas() / get_tool_functions()：
        quest agent 自身引用这两个方法拼装工具表，混入会自调用递归；
        由各 agent（PFCA / proactive / 反思等）按需单独拼装接入。
        """
        return {
            "type": "function",
            "function": {
                "name": "quest_agent",
                "description": (
                    "直接联系 quest agent（任务管理与推进裁决者），转交一段内容由它裁决处理。\n"
                    "适用：请它审批某任务的信息/材料/时机是否就绪、能否推进下一阶段；"
                    "请它登记创建一个新任务；请它修订/推进/完结一个已有任务。\n"
                    "是否立项、阶段如何拆解、能否推进均由 quest agent 独立裁决（可能驳回），"
                    "你只负责转交事实与诉求，不要替它下结论。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": (
                                "转交给 quest agent 的内容：任务前因后果、相关人物、"
                                "当前进展与所缺（信息/材料/时机）、希望它做什么"
                                "（审批推进 / 创建任务 / 修订任务）。"
                                "涉及已有任务时必须注明具体任务标题。"
                            ),
                        },
                    },
                    "required": ["content"],
                },
            },
        }

    @classmethod
    def get_task_tool_schemas(cls) -> List[Dict[str, Any]]:
        """返回通用级任务工具的 schema 列表（供 agent 工具表直接拼装）。

        仅含通用级（append_task_log / query_task_status）；
        裁决级（create_task / update_task_status）的 schema 私有于 QuestAgent；
        quest_agent 入口通道不在此列（quest agent 自身也引用本列表，混入会自调用递归，
        由各 agent 按需经 get_quest_agent_tool_schema() 单独拼装接入）。
        """
        return [
            cls.get_append_task_log_tool_schema(),
            cls.get_query_task_status_tool_schema(),
        ]

    # ============ 工具实现 ============

    @classmethod
    async def query_task_status(
        cls,
        task_title: Optional[str] = None,
    ) -> Dict[str, Any]:
        """查询任务状态：指定标题查细节，未指定查所有未完成任务。

        Returns:
            {"task_title": Optional[str], "detail": str}
        """
        from brain.hc.database.task import TaskOperations

        logger.info(f"查询任务: task_title={task_title!r}")

        task_ops = TaskOperations()

        if task_title:
            task_id = await task_ops.get_task_id_by_title(task_title)
            if not task_id:
                detail = f"未找到标题为「{task_title}」的任务"
            else:
                detail = await task_ops.format_task_by_id(task_id)
            return {"task_title": task_title, "detail": detail}

        task_ids = await task_ops.get_active_tasks(limit=50)
        if not task_ids:
            detail = "当前没有未完成的任务"
        else:
            detail = await task_ops.format_tasks_by_ids(task_ids)
        return {"task_title": None, "detail": detail}

    @classmethod
    async def append_task_log(cls, task_title: str, content: str) -> Dict[str, Any]:
        """给任务添加一条笔记（记录级低权限：只添加，不改阶段、不完结）。

        委托 TaskOperations.append_task_log；任务不存在或已完结时返回
        success=False（业务级失败，由调用方决定如何反馈）。
        """
        from brain.hc.database.task import TaskOperations

        logger.info(f"追加任务记录: task_title={task_title}, content={content}")

        task_ops = TaskOperations()
        success = await task_ops.append_task_log(task_title, content)

        return {
            "task_title": task_title,
            "content": content,
            "success": success,
        }

    # ============ quest agent 联系通道 ============

    # 进程级持久持有的 QuestAgent 实例（懒加载单例，跨事件链延续上下文；
    # 持久化语义与 ToolPersistent 对齐：上下文随调用累积属有意取舍）
    _quest_agent = None

    @classmethod
    def _get_quest_agent(cls):
        """获取进程级持久持有的 QuestAgent 实例（懒加载单例）。

        获取失败直接上抛崩溃阻断（硬约定），不吞。"""
        if cls._quest_agent is None:
            from brain.base.light_agent.quest_agent import QuestAgent
            cls._quest_agent = QuestAgent()
            logger.debug("ToolTask: 新建持久化 QuestAgent（进程级单例）")
        return cls._quest_agent

    @classmethod
    async def quest_agent(cls, content: str) -> str:
        """直接联系 quest agent：转交一段内容，由其多轮 LLM 决策后给出裁决性结论。

        本工具只是联系通道，不做任何任务写操作；创建任务、概况重写、阶段推进、
        完结等裁决级写权限仍独占于 quest agent 本体（由其在 run() 内经私有工具
        执行）。quest agent 会独立判断是否遵从转交的诉求，不盲从（可能驳回）。

        Returns:
            str: quest agent 的最终裁决结论文本（先结论后依据）
        """
        from brain.hc.global_session_state import get_global_session_state

        logger.info(f"联系 quest agent: content={content}")

        focus_state = get_global_session_state()
        focus_state.manager_pfca_actions('add', f"转交 quest agent：{content}")

        agent = cls._get_quest_agent()
        result = await agent.run(additional_content=content)

        if isinstance(result, dict) and result.get('formatted_output'):
            return result['formatted_output']
        return str(result) if result else "quest agent 无输出"

    # ============ 分发表 ============

    @classmethod
    def get_tool_functions(cls) -> Dict[str, Callable]:
        """返回通用级工具名 -> 实现的分发表（供 agent 的 _initialize_tool_functions 直接使用）。

        裁决级（create_task / update_task_status）不在此表，私有于 QuestAgent；
        quest_agent 入口通道亦不在此表（quest agent 自身引用本表，混入会自调用递归），
        由各 agent 按需映射 {"quest_agent": ToolTask.quest_agent} 接入。
        """
        return {
            "append_task_log": cls.append_task_log,
            "query_task_status": cls.query_task_status,
        }
