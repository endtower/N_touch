# -*- coding: utf-8 -*-
"""通用工具：ToolTask（任务管理工具：create_task / update_task_status / query_task_status）

任务系统收口模块（2026-09-15 由 tool_event.py 事件系统重做而来）：
schema 与实现统一在此管理，供 TaskRegisterAgent 工具表直接拼装、
以及 proactive 等模块绕过 agent 直调底层实现，避免复制漂移。

工具语义：
- create_task：为一个独立主题创建一个新任务（标题 + 概况 + 下次发生时间）
- update_task_status：修改任务状态——用新的完整概况覆盖旧的，并更新下次发生时间；
  下次发生时间输入「完结」即标记该任务已完成
- query_task_status：查询任务状态——不传标题返回所有未完成任务列表；
  传入具体标题返回该任务细节

全异步：底层走 TaskOperations（纯异步），本模块不引入同步 IO；
失败直接上抛崩溃阻断（硬约定），不包 async_handle_errors 吞错。
"""

from typing import Dict, Any, List, Callable, Optional

from utils.logger import get_logger

logger = get_logger('ToolTask', "DEBUG")


class ToolTask:
    """任务管理通用工具：create_task + update_task_status + query_task_status。"""

    # ============ 工具 schema（提示词） ============

    @classmethod
    def get_create_task_tool_schema(cls) -> Dict[str, Any]:
        """返回 create_task 工具的 schema。"""
        return {
            "type": "function",
            "function": {
                "name": "create_task",
                "description": (
                    "创建一个需要下次唤醒才能完成的任务。\n"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "任务标题，简短概括任务",
                        },
                        "summary": {
                            "type": "string",
                            "description": "任务概况，详细描述任务内容（少于100个字符）",
                        },
                        "next_occurrence": {
                            "type": "string",
                            "description": (
                                "下次发生时间，ISO 8601格式，如：2024-01-20T10:00:00"
                            ),
                        },
                    },
                    "required": ["title", "summary", "next_occurrence"],
                },
            },
        }

    @classmethod
    def get_update_task_status_tool_schema(cls) -> Dict[str, Any]:
        """返回 update_task_status 工具的 schema。"""
        return {
            "type": "function",
            "function": {
                "name": "update_task_status",
                "description": (
                    "修改一个已有任务的状态。\n"
                    "用于：(1) 用新的完整概况覆盖旧的概况（记录任务当前进展）；"
                    "(2) 调整下次发生时间；"
                    "(3) 完结任务——下次发生时间输入「完结」即标记该任务已经完成。\n"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_title": {
                            "type": "string",
                            "description": "要修改的任务标题，用于定位任务",
                        },
                        "summary": {
                            "type": "string",
                            "description": (
                                "新的完整概况（写一个新的概况，覆盖旧的），"
                                "描述任务当前状态或新进展"
                            ),
                        },
                        "next_occurrence": {
                            "type": "string",
                            "description": (
                                "下次发生时间。ISO 8601格式（如 2024-01-20T10:00:00）；"
                                "输入「完结」则标记该任务已经完成"
                            ),
                        },
                    },
                    "required": ["task_title", "summary", "next_occurrence"],
                },
            },
        }

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
    def get_task_tool_schemas(cls) -> List[Dict[str, Any]]:
        """返回任务工具的 schema 列表（供 agent 工具表直接拼装）。"""
        return [
            cls.get_create_task_tool_schema(),
            cls.get_update_task_status_tool_schema(),
            cls.get_query_task_status_tool_schema(),
        ]

    # ============ 工具实现 ============

    @classmethod
    async def create_task(
        cls,
        title: str,
        summary: str,
        next_occurrence: Optional[str] = None,
    ) -> Dict[str, Any]:
        """创建一个新任务。

        Returns:
            {"task_id": str, "title": str, "summary": str, "next_occurrence": str}
        """
        from brain.hc.database.task import TaskOperations

        logger.info(f"创建任务: title={title}, next_occurrence={next_occurrence}")

        task_ops = TaskOperations()
        task_id = await task_ops.create_task(
            title=title,
            summary=summary,
            next_occurrence=next_occurrence,
        )

        logger.info(f"任务创建成功: task_id={task_id}")

        return {
            "task_id": task_id,
            "title": title,
            "summary": summary,
            "next_occurrence": next_occurrence,
        }

    @classmethod
    async def update_task_status(
        cls,
        task_title: str,
        summary: str,
        next_occurrence: Optional[str] = None,
    ) -> Dict[str, Any]:
        """修改任务状态：覆盖概况 + 更新下次发生时间。

        next_occurrence 含「完结」时标记任务已完成（写 end_time），
        否则保持进行中并更新下次发生时间。

        Returns:
            {"task_title": str, "summary": str, "next_occurrence": str,
             "status": str, "success": bool}
        """
        from brain.hc.database.task import (
            TaskOperations, TASK_STATUS_ACTIVE, TASK_STATUS_COMPLETED,
        )

        logger.info(f"更新任务: task_title={task_title}, next_occurrence={next_occurrence}")

        if next_occurrence and "完结" in next_occurrence:
            status = TASK_STATUS_COMPLETED
            next_occurrence_val = ""
        else:
            status = TASK_STATUS_ACTIVE
            next_occurrence_val = next_occurrence or ""

        task_ops = TaskOperations()
        success = await task_ops.update_task(
            task_title,
            summary=summary,
            status=status,
            next_occurrence=next_occurrence_val,
        )

        if success:
            logger.info(f"任务更新成功: {task_title} (status={status})")
        else:
            logger.warning(f"任务更新失败，可能找不到: {task_title}")

        return {
            "task_title": task_title,
            "summary": summary,
            "next_occurrence": next_occurrence_val,
            "status": status,
            "success": success,
        }

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

    # ============ 分发表 ============

    @classmethod
    def get_tool_functions(cls) -> Dict[str, Callable]:
        """返回工具名 -> 实现的分发表（供 agent 的 _initialize_tool_functions 直接使用）。"""
        return {
            "create_task": cls.create_task,
            "update_task_status": cls.update_task_status,
            "query_task_status": cls.query_task_status,
        }
