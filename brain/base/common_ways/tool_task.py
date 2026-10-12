# -*- coding: utf-8 -*-
"""跨事件链接续事项工具。

query_task_status 不传标题返回有界简表，指定标题返回完整记录。
quest_agent 转交「标题 + 要传递给下次启动时的上下文 + 下次唤醒时间」给 Quest，
由其查重后登记（重复则不登记）。Quest 不审批行动策略，也不做更新/阶段推进。
记录写权限保留于 Quest，行动与完成判断归调用方。
"""

from typing import Dict, Any, List, Callable, Optional

from utils.logger import get_logger

logger = get_logger('ToolTask', "DEBUG")


class ToolTask:
    """任务管理通用级工具：query_task_status + quest_agent（联系通道）。"""

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
                    "不传 task_title 时返回待接续目录（最多 20 条及总数）；"
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
    def get_quest_agent_tool_schema(cls) -> Dict[str, Any]:
        """返回 quest_agent 工具的 schema（联系 quest agent 的入口通道）。

        不进 get_task_tool_schemas() / get_tool_functions()：
        quest agent 自身引用这两个方法拼装工具表，混入会自调用递归；
        由各 agent（PFCA / proactive / 反思等）按需单独拼装接入。
        """
        return {
            "type": "function",
            "function": {
                "name": "quest_agent",
                "description": (
                    "请 Quest 检查这条事项是否与已有待接续事项重复；不重复则登记一条新事项，"
                    "重复则不予登记。适用：择时继续、跟进结果、保存未完事项。"
                    "你需给出标题、要传递给下次启动时的上下文与下次唤醒时间。"
                    "Quest 只做查重与登记，不审批行动策略。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "事项标题，简短概括这条待接续事项。",
                        },
                        "context": {
                            "type": "string",
                            "description": (
                                "要传递给下次启动时的上下文：这条事项的目的、已确认进展、"
                                "人物/会话/材料线索、下次要处理什么。"
                            ),
                        },
                        "next_occurrence": {
                            "type": "string",
                            "description": (
                                "下次唤醒时间：ISO 8601（如 2024-01-20T10:00:00），"
                                "或「尽快」表示现在执行。"
                            ),
                        },
                    },
                    "required": ["title", "context", "next_occurrence"],
                },
            },
        }

    @classmethod
    def get_task_tool_schemas(cls) -> List[Dict[str, Any]]:
        """返回通用级任务工具的 schema 列表（供 agent 工具表直接拼装）。

        仅含通用级（query_task_status）；
        记录写入级（create_task）的 schema 私有于 QuestAgent；
        quest_agent 入口通道不在此列（quest agent 自身也引用本列表，混入会自调用递归，
        由各 agent 按需经 get_quest_agent_tool_schema() 单独拼装接入）。
        """
        return [
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

        detail = await task_ops.format_active_task_overview(limit=20)
        if not detail:
            detail = "当前没有待接续事项"
        return {"task_title": None, "detail": detail}

    # ============ quest agent 联系通道 ============

    # 每次请求新建工作实例，长期接续仅依赖任务记录。
    @classmethod
    def _get_quest_agent(cls):
        """创建独立工作实例，避免并发请求或旧调用污染上下文。"""
        from brain.base.light_agent.quest_agent import QuestAgent
        return QuestAgent()

    @classmethod
    async def quest_agent(cls, title: str, context: str, next_occurrence: str) -> str:
        """转交一条待接续事项给 Quest 查重登记，返回本次实际操作回执。"""
        result = await cls._run_quest_request(title, context, next_occurrence)
        if isinstance(result, dict) and result.get('formatted_output'):
            return result['formatted_output']
        return str(result) if result else "quest agent 无输出"

    @classmethod
    async def _run_quest_request(cls, title: str, context: str,
                                 next_occurrence: str) -> Dict[str, Any]:
        """后端调用入口：保留本次任务登记结果；通用 LLM 工具仍返回原结论文本。"""
        from brain.hc.global_session_state import get_global_session_state

        logger.info(f"联系 quest agent: title={title}, next_occurrence={next_occurrence}")

        focus_state = get_global_session_state()
        focus_state.manager_pfca_actions('add', f"转交 quest agent：{title}")

        agent = cls._get_quest_agent()
        return await agent.run(title=title, context=context, next_occurrence=next_occurrence)

    # ============ 分发表 ============

    @classmethod
    def get_tool_functions(cls) -> Dict[str, Callable]:
        """返回通用级工具名 -> 实现的分发表（供 agent 的 _initialize_tool_functions 直接使用）。

        记录写入级（create_task）不在此表，私有于 QuestAgent；
        quest_agent 入口通道亦不在此表（quest agent 自身引用本表，混入会自调用递归），
        由各 agent 按需映射 {"quest_agent": ToolTask.quest_agent} 接入。
        """
        return {
            "query_task_status": cls.query_task_status,
        }
