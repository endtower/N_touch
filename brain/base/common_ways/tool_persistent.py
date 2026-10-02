# -*- coding: utf-8 -*-
"""通用工具：ToolPersistent（持久化工具：memory_agent / quest_agent）

原 PFCA 侧实现（pfca_tools.memory_adjustment / event_judgement）于 2026-09-15
收口迁移至本模块，schema 与实现统一在此管理，避免各处复制漂移：
- memory_agent：修改对目标的基础信息印象（委托 MemoryAgent，写入短期记忆与通讯录）
- quest_agent：长期任务（Quest）管理的唯一入口（委托 QuestAgent 多轮 LLM 决策：
  阶段拆解 / 进展追踪 / 推进裁决；进行中任务 + next_occurrence 到点自动唤醒）。
  2026-09-17 由 task_register_agent 任务登记升级而来。

持久化语义：
- QuestAgent 实例由本模块进程级持久持有（单例），跨事件链延续
  messages / backlog / tools_called 上下文，不再由调用方（PFCA 等）在链启动时
  清空重建。注意：agent 上下文随调用累积增长，属持久化设计的有意取舍。
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
    _quest_agent = None

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
        """返回 quest_agent 工具的 schema（任务系统，2026-09-17 由任务登记升级为任务管理）。"""
        return {
            "type": "function",
            "function": {
                "name": "quest_agent",
                "description": (
                    "长期任务（Quest）管理：任务的阶段拆解（滚动式）、进展追踪"
                    "（当前到哪一步）与推进裁决（系统当前是否有能力推进下一阶段）。"
                    "不产出具体策略与话术。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": (
                                "任务前因后果、相关人物、下次处理时间、最终汇报时间等。"
                                "进行中的任务必须注明具体未完成任务与阶段进展。"
                            ),
                        },
                        "operation_type": {
                            "type": "string",
                            "enum": ["create_task", "update_task", "review_stage"],
                            "description": (
                                "create_task=新建任务并拆解近期阶段；"
                                "update_task=推进或修订已有任务；"
                                "review_stage=阶段裁决（进展到哪一步、下一阶段能否推进）。"
                            )
                        }
                    },
                    "required": ["content", "operation_type"]
                }
            }
        }

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
        """获取进程级持久持有的 QuestAgent 实例（懒加载单例）。

        获取失败直接上抛崩溃阻断（硬约定 #4），不吞。"""
        if cls._quest_agent is None:
            from brain.base.light_agent.quest_agent import QuestAgent
            cls._quest_agent = QuestAgent()
            logger.debug("ToolPersistent: 新建持久化 QuestAgent（进程级单例）")
        return cls._quest_agent

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
    async def quest_agent(cls, content: str, operation_type: str) -> str:
        """长期任务（Quest）管理（2026-09-17 由 task_register_agent 任务登记升级）。

        与旧版差异：QuestAgent 实例由本模块进程级持久持有（跨事件链
        延续上下文），不再由调用方在链启动时清空重建。

        Args:
            content: 具体情况的详细描述
            operation_type: 操作类型
                - 'create_task': 创建新任务并拆解近期阶段
                - 'update_task': 推进或修订一个已有任务
                - 'review_stage': 阶段裁决（进展到哪一步、下一阶段能否推进）

        Returns:
            str: QuestAgent 的最终文本回复（未使用工具的轮次）
        """
        logger.info(f"执行quest_agent操作，operation_type={operation_type}，内容: {content}")

        from brain.hc.global_session_state import get_global_session_state

        focus_state = get_global_session_state()
        action_desc = {
            "create_task": f"新增任务：{content}",
            "update_task": f"更新任务：{content}",
            "review_stage": f"阶段裁决：{content}",
        }.get(operation_type, f"记录任务：{content}")
        focus_state.manager_pfca_actions('add', action_desc)

        # 复用持久持有的 QuestAgent 实例：多次调用时 agent 的
        # messages / backlog 上下文是连续的（持久化语义）。
        agent = cls._get_quest_agent()
        result = await agent.run(additional_content=content, operation_type=operation_type)

        if isinstance(result, dict) and result.get('formatted_output'):
            return result['formatted_output']
        return str(result) if result else "任务已记录"
