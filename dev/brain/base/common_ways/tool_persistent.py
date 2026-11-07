# -*- coding: utf-8 -*-
"""通用工具：ToolPersistent（持久化工具：memory_agent / quest_agent）

原 PFCA 侧实现（pfca_tools.memory_adjustment / event_judgement）于 2026-09-15
收口迁移至本模块，schema 与实现统一在此管理，避免各处复制漂移：
- memory_agent：修改对目标的基础信息印象（委托 MemoryAgent，写入短期记忆与通讯录）
- quest_agent：接续入口，复用 ToolTask 的职责说明（标题 + 上下文 + 唤醒时间，
  查重后登记，重复则不登记）。

持久化语义：
- Quest 的长期信息保存于任务数据库，每次调用新建工作实例。
- MemoryAgent 每次调用新建（其 run() 为单发多轮，无跨调用状态）。

调用方接入方式（PFCA 暂时移除接入，2026-09-15；恢复时按此接回）：
- get_tools() 拼装 ToolPersistent.get_persistent_tool_schemas()
- 分发表映射 {"memory_agent": ToolPersistent.memory_agent,
  "quest_agent": ToolPersistent.quest_agent}
"""

from typing import Dict, Any, List

from utils.logger import get_logger

logger = get_logger('ToolPersistent', "INFO")


class ToolPersistent:
    """持久化通用工具：memory_agent（印象记忆）+ quest_agent（任务管理）。"""

    # 进程级持久持有的 QuestAgent 实例（跨事件链共享上下文，懒加载）

    # ============ 工具 schema（提示词） ============

    @classmethod
    def get_memory_agent_tool_schema(cls) -> Dict[str, Any]:
        """返回 memory_agent 工具的 schema（原样迁自 pfca_tools，2026-09-15）。"""
        return {
            "type": "function",
            "function": {
                "name": "memory_agent",
                "description": "管理角色第一印象与关系信息（写入通讯录的角色说明 / 短期记忆中的角色群聊信息）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "会话 id，格式 'type_id'。"
                        },
                        "modification_type": {
                            "type": "string",
                            "enum": ["session", "mentioned_person"],
                            "description": "'session' 修改目标会话印象；'mentioned_person' 创建会话中被提及的重要人物。"
                        },
                        "content": {
                            "type": "string",
                            "description": "自然语言描述要修改的方向。"
                        },
                        "extra_name": {
                            "type": "string",
                            "description": "仅 modification_type='mentioned_person' 时必填，被提及人物的名字。"
                        }
                    },
                    "required": ["session_id", "modification_type", "content"]
                }
            }
        }

    @classmethod
    def get_quest_agent_tool_schema(cls) -> Dict[str, Any]:
        """复用接续工具说明（标题 + 上下文 + 唤醒时间）。"""
        from brain.base.common_ways.tool_task import ToolTask
        return ToolTask.get_quest_agent_tool_schema()

    @classmethod
    def get_persistent_tool_schemas(cls) -> List[Dict[str, Any]]:
        """返回两个持久化工具的 schema 列表（供工具表直接拼装）。"""
        return [
            cls.get_memory_agent_tool_schema(),
            cls.get_quest_agent_tool_schema(),
        ]

    # ============ 持久化实例管理 ============

    @classmethod
    def _get_quest_agent(cls):
        """兼容旧入口，使用独立工作实例。"""
        from brain.base.common_ways.tool_task import ToolTask
        return ToolTask._get_quest_agent()

    # ============ 通用执行方法 ============

    @classmethod
    async def memory_agent(cls, session_id: str, modification_type: str, content: str, extra_name: str = "") -> str:
        """调整或创建记忆（原样迁自 PFCA_tool_manager.memory_adjustment）。

        Args:
            session_id: 发生的会话ID（在哪一个会话的会谈中被提及的）。
                        格式为 'type_id'，例如 'private_1234567890' / 'group_198431299'。
                        系统会根据该 ID 自动判定是群聊（Group）还是私聊（Session/Character）。
            modification_type: 修改类型。
                        'session'：修改本会话印象（兼容群聊/私聊）。
                        'mentioned_person'：创建一个在该会话中被提及的重要人物。
            content: 自然语言描述修改的值。
            extra_name: 仅当 modification_type='mentioned_person' 时必填，被提及人物的名字。

        Returns:
            str: Agent 的文本回复
        """
        # 参数校验
        if not session_id:
            return "[错误] memory_agent 缺少必填参数 session_id"
        if modification_type not in ("session", "mentioned_person"):
            return f"[错误] memory_agent 的 modification_type 必须是 'session' 或 'mentioned_person'，收到: {modification_type}"
        if not content:
            return "[错误] memory_agent 缺少必填参数 content"
        if modification_type == "mentioned_person" and not extra_name:
            return "[错误] modification_type='mentioned_person' 时必须提供 extra_name（被提及人物的名字）"

        logger.info(
            f"执行memory_agent操作: session_id={session_id}, "
            f"modification_type={modification_type}, content={content}, extra_name={extra_name}"
        )

        from brain.hc.global_session_state import get_global_session_state
        from brain.base.light_agent.memory_agent import MemoryAgent

        # 事件链由 station 在调 async_process_from_att_factory 前统一启动，
        # 这里不再启动事件链（与 station 的事件链生命周期对齐）。
        focus_state = get_global_session_state()

        # 记录到行动日志
        if modification_type == "mentioned_person":
            pfca_action = f"记录被提及人物「{extra_name}」（来源会话 {session_id}）：{content}"
        else:
            pfca_action = f"调整会话「{session_id}」印象：{content}"
        focus_state.manager_pfca_actions('add', pfca_action)
        logger.info(f"添加pfca action: {pfca_action}")

        agent = MemoryAgent()
        result = await agent.run(
            session_id=session_id,
            modification_type=modification_type,
            content=content,
            extra_name=extra_name or None,
        )
        return str(result)

    @classmethod
    async def quest_agent(cls, title: str, context: str, next_occurrence: str = "",
                          kind: str = "onetime") -> str:
        """转交一条待接续事项给 Quest 查重登记。"""
        agent = cls._get_quest_agent()
        result = await agent.run(
            title=title, context=context,
            next_occurrence=next_occurrence, kind=kind,
        )
        if isinstance(result, dict) and result.get('formatted_output'):
            return result['formatted_output']
        return str(result) if result else "Quest 无操作回执"
