# -*- coding: utf-8 -*-
"""通用工具：ToolSocial（社交环境查询工具）

通讯录与会话历史查询的唯一实现，收口于本模块（pfca / proactive /
persona / reflection 的工具表均委托这里，pfca_tools.py 本地版本已删除）：
- query_address_book：查看通讯录（可联系的会话 / 群聊），或直接查询指定角色的印象
- query_history：查询指定会话的历史聊天记录。两种拉取模式二选一：
  数量模式（search_range=last_n / all）或时间范围模式（time_range，相对时长或
  绝对区间）；可选 speaker 筛选目标人物的发言。返回带 #seq 序号，
  会话不存在时列出可用会话供 LLM 自助纠正。consumer_id 默认 None＝纯只读、
  不推进任何游标（reflection 等只读核实场景复用）；需要标记"已读"的场景
  （pfca / proactive / persona 显式传 "pfca"，social_chat /
  conversation 传 CONVERSATION_CONSUMER_ID）才推进游标——且仅无过滤的
  最新拉取才推进（speaker / time_range 查询不动游标，详见方法 docstring）

schema 与实现统一收口在本模块，pfca / proactive / reflection 的工具注册表与
persona_agent 的工具表均引用此处，避免各处复制漂移。
"""

import re
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

from utils.logger import get_logger

logger = get_logger('ToolSocial', "INFO")


class ToolSocial:
    """社交环境查询通用工具：通讯录 + 会话历史。"""

    # ============ 工具 schema（提示词） ============

    @classmethod
    def get_query_address_book_tool_schema(cls) -> Dict[str, Any]:
        """返回 query_address_book 工具的 schema（对齐 PFCA 版）。"""
        return {
            "type": "function",
            "function": {
                "name": "query_address_book",
                "description": (
                    "查看通讯录（可联系的会话 / 群聊），或直接查询指定角色的印象。"
                    "查找范围可选「私聊」「群聊」「全部」；也可以输入具体角色名称，"
                    "此时将直接查询该角色在记忆中的印象。"
                    "默认只返回各会话的联系方式与印象；如需了解某会话的近期状态"
                    "（活跃/不活跃、消息总数、最后一条消息），请将 include_status 置为 true。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "search_range": {
                            "type": "string",
                            "description": "查找范围：「私聊」「群聊」「全部」之一，或具体角色名称（如「主人」）。输入具体名称时，将直接查询该角色的印象。"
                        },
                        "include_status": {
                            "type": "boolean",
                            "description": "是否附带该会话的近期状态（活跃/不活跃、共多少条消息、最后一条消息内容）。默认 false：只返回联系方式和印象。"
                        }
                    },
                    "required": ["search_range"]
                }
            }
        }

    @classmethod
    def get_query_history_tool_schema(cls) -> Dict[str, Any]:
        """返回 query_history 工具的 schema。"""
        return {
            "type": "function",
            "function": {
                "name": "query_history",
                "description": (
                    "查询某个会话的历史聊天记录。"
                    "返回按时间正序排列、带序号(#seq)与时间戳的消息列表。"
                    "两种拉取模式二选一：看最近 N 条用 search_range（数量模式），"
                    "看某段时间的记录用 time_range（时间范围模式，传入后忽略 search_range）；"
                    "可选 speaker 只看目标人物的发言。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "目标会话 id，格式 'type_id'。不知道确切 id 时先用 query_address_book 查。"
                        },
                        "search_range": {
                            "type": "string",
                            "description": (
                                "数量模式拉取条数：'last_n' 表示最新 n 条（如 'last_20'，默认 last_20），"
                                "'all' 表示尽量多拉（上限 100 条）。传了 time_range 时忽略本参数。"
                            )
                        },
                        "time_range": {
                            "type": "string",
                            "description": (
                                "时间范围模式：相对时长如 '30m' / '2h' / '3d'（最近该时段），"
                                "或绝对区间 'YYYY-MM-DD HH:MM~YYYY-MM-DD HH:MM'（一端可省略，"
                                "如 '2026-09-18 10:00~' 表示从该时刻至今）。传入后拉取该时段内消息。"
                            )
                        },
                        "speaker": {
                            "type": "string",
                            "description": (
                                "可选，筛选目标人物的发言：只返回该人物发出的消息"
                                "（名字模糊匹配，可与数量 / 时间范围组合，如查某人最近说了什么）。"
                            )
                        }
                    },
                    "required": ["session_id"]
                }
            }
        }

    @classmethod
    def get_query_tool_schemas(cls) -> List[Dict[str, Any]]:
        """返回两个社交查询工具的 schema 列表（供工具表直接拼装）。"""
        return [
            cls.get_query_address_book_tool_schema(),
            cls.get_query_history_tool_schema(),
        ]

    # ============ 通用执行方法 ============

    @classmethod
    async def query_address_book(cls, search_range: str, include_status: bool = False) -> str:
        """查询通讯录 / 指定角色的印象（对齐 PFCA_tool_manager._query_address_book）。

        Args:
            search_range: 查找范围。可选'私聊'、'群聊'、'全部'；
                          或直接输入具体角色名称，此时将直接查询该角色的印象。
            include_status: 是否附带近期状态（活跃/不活跃、消息总数、最后一条消息）。
                           默认 False：只返回联系方式与印象。

        Returns:
            str: 通讯录信息；输入具体名称时返回该角色的印象信息。
        """
        logger.debug(f"执行query_address_book操作，查找范围: {search_range}, include_status={include_status}")

        from memory.address_book import get_address_book
        address_book = get_address_book()

        if search_range == '私聊':
            return await address_book.get_private_contacts(include_status=include_status)
        if search_range == '群聊':
            return await address_book.get_group_contacts(include_status=include_status)
        if search_range == '全部':
            return await address_book.get_all_contacts(include_status=include_status)

        # 其他输入视为具体角色名称 → 直接查询该角色的印象
        from brain.hc.database.character_compose import CharacterCompose
        character_compose = CharacterCompose()
        impression_result = await character_compose.first_session_impression_search(name=search_range)
        formatted_info = impression_result.get(
            "formatted_info", f"你对{search_range}没有任何印象。"
        )
        return f"你查询了「{search_range}」的印象：\n{formatted_info}"

    @classmethod
    async def query_history(
        cls,
        session_id: str,
        search_range: str = "last_20",
        time_range: Optional[str] = None,
        speaker: Optional[str] = None,
        consumer_id: Optional[str] = None,
    ) -> str:
        """查询指定会话的历史聊天记录（通用方法，只读为主）。

        两种拉取模式二选一 + 可选人物筛选：
        - 数量模式（默认）：last_n / all，拉最新 n 条（默认 last_20；all 上限 100 条）
        - 时间范围模式（传 time_range）：拉取时段内的消息（相对时长或绝对区间），
          传入后忽略 search_range
        - speaker：只看目标人物的发言，可与任一模式组合
          （"某人最近说了什么" = last_n + speaker；"过去2小时某人说了什么" = time_range + speaker）

        游标语义：仅"无 speaker 且无 time_range 的最新拉取"才推进 consumer_id
        游标（此时拉到的就是会话最新消息，标记已读是准确的）；人物筛选 /
        时间范围查询不动任何游标（LLM 没有完整看到最新消息，不能替消费者标记已读）。

        Args:
            session_id: 会话标识，格式为 "type_id"，如 "private_1234567890" / "group_198431299"
            search_range: 数量模式拉取条数，'last_n'（如 'last_20'，默认 'last_20'）或 'all'
            time_range: 时间范围（见 _parse_time_range 支持的格式）；None＝数量模式
            speaker: 可选，筛选目标人物的发言（user_name 模糊匹配）
            consumer_id: 消费者游标标识。None（默认）＝纯只读，不推进任何游标
                （reflection 只做事实核实，读历史不得影响未读检测）；
                需要标记"已读"的场景须显式传入：pfca / proactive /
                persona 传 "pfca"（LLM 已读到这些消息，truck
                未读检测不再重复触发本会话），conversation / social_chat
                传 CONVERSATION_CONSUMER_ID 共用 conversation 游标

        Returns:
            str: 带会话元信息头、#seq 序号的聊天记录文本
        """
        logger.debug(
            f"执行历史记录查询: session={session_id} range={search_range} "
            f"time_range={time_range!r} speaker={speaker!r}"
        )

        # ---- 参数解析（工具边界校验，错误信息供 LLM 自纠） ----
        message_count = cls._parse_search_range(search_range)
        start_ts: Optional[float] = None
        end_ts: Optional[float] = None
        if time_range is not None:
            start_ts, end_ts = cls._parse_time_range(time_range)
        speaker_filter = speaker.strip() if speaker else None
        if speaker is not None and not speaker_filter:
            raise ValueError("speaker 不能为空字符串；不需要筛选发言人时请省略该参数。")

        from memory.message_repository import get_message_store
        from memory.session_history import (
            get_global_session_history,
            FetchOptions,
            MessageFormatOptions,
        )

        # 用单例（测试隔离库/真实库），勿新建默认路径实例
        store = get_message_store()

        # ---- 会话存在性校验：不存在时列出可用会话，供 LLM 自助纠正 ----
        if not await store.session_exists(session_id):
            return await cls._unknown_session_reply(session_id)

        # ---- 拉取：时间范围模式拉满上限；数量模式按 search_range ----
        limit = cls.MAX_HISTORY_PULL if time_range is not None else message_count
        result = await store.query_session_messages(
            session_id, limit=limit, speaker=speaker_filter,
            start_ts=start_ts, end_ts=end_ts,
        )
        messages = result["messages"]
        total_hits = result["total_hits"]
        total_count = result["total_count"]

        # ---- 游标推进：仅无过滤的最新拉取（详见 docstring 游标语义） ----
        if consumer_id is not None and messages and speaker_filter is None and time_range is None:
            target_seq = max(m.get("_seq") or -1 for m in messages)
            current = await store.get_cursor(session_id, consumer_id)
            if target_seq > current:
                await store.advance_cursor(session_id, consumer_id, target_seq)
                logger.info(
                    f"query_history 已推进 {consumer_id} 游标 {current} -> {target_seq} ({session_id})"
                )

        # ---- 格式化：复用全局格式化原语（时间戳 + 名字），逐行补 #seq ----
        if not messages:
            if speaker_filter and time_range is not None:
                return (
                    f"会话 {session_id} 共 {total_count} 条消息，"
                    f"但时间范围 {time_range} 内没有「{speaker_filter}」的发言。"
                )
            if speaker_filter:
                return (
                    f"会话 {session_id} 共 {total_count} 条消息，"
                    f"没有匹配「{speaker_filter}」的发言记录（可尝试其他称呼写法）。"
                )
            if time_range is not None:
                return f"会话 {session_id} 在时间范围 {time_range} 内没有消息（共 {total_count} 条）。"
            return f"会话 {session_id} 存在但暂无消息。"

        history = get_global_session_history()
        formatted_lines, _ = history.format_messages(
            messages,
            FetchOptions(
                message_format=MessageFormatOptions(
                    show_new_marker=False,
                    show_timestamp=True
                )
            )
        )
        seq_lines = [
            f"#{m.get('_seq')} {line}" for m, line in zip(messages, formatted_lines)
        ]

        # ---- 结果头 ----
        if speaker_filter or time_range is not None:
            desc_parts = []
            if speaker_filter:
                desc_parts.append(f"筛选「{speaker_filter}」的发言")
            if time_range is not None:
                desc_parts.append(f"时间范围 {time_range}")
            shown = (
                f"｜按时间正序显示最新 {len(messages)} 条"
                if total_hits > len(messages) else ""
            )
            header = (
                f"[会话 {session_id}｜{'、'.join(desc_parts)}｜命中 {total_hits} 条"
                f"（会话共 {total_count} 条消息）{shown}]"
            )
        else:
            header = (
                f"[会话 {session_id}｜共 {total_count} 条消息｜"
                f"按时间正序显示最新 {len(messages)} 条]"
            )

        # ---- 截断提示尾：命中多于显示时提示缩小范围看更早内容 ----
        footers = []
        if total_hits > len(messages):
            footers.append(
                f"（命中 {total_hits} 条仅显示最新 {len(messages)} 条，"
                f"需要更早内容时缩小时间范围）"
            )

        parts = [header] + seq_lines + footers
        chat_result = "\n".join(parts)
        logger.debug(f"历史记录查询模块返回结果: {chat_result}")
        return chat_result

    # --------------------------------------------------------
    # query_history 内部辅助
    # --------------------------------------------------------

    # 单次拉取上限：保护 LLM 上下文，防止一次拉爆
    MAX_HISTORY_PULL = 100

    # 绝对时间解析格式（按本地时区解释，与消息 timestamp 写入侧同为 epoch 秒）
    _TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H", "%Y-%m-%d")

    @classmethod
    def _parse_search_range(cls, search_range: str) -> int:
        """解析 search_range（'last_n' / 'all'），返回条数；非法格式抛异常阻断。"""
        if search_range is None or not str(search_range).strip():
            return 20
        text = str(search_range).strip()
        if text == "all":
            return cls.MAX_HISTORY_PULL
        m = re.fullmatch(r'last_(\d+)', text)
        if m:
            n = int(m.group(1))
            if n < 1:
                raise ValueError(f"search_range 非法: {search_range!r}（条数必须 >= 1）")
            return min(n, cls.MAX_HISTORY_PULL)
        raise ValueError(
            f"search_range 非法: {search_range!r}（须为 'last_n' 如 'last_20'，或 'all'）"
        )

    @classmethod
    def _parse_time_range(cls, time_range: str) -> Tuple[Optional[float], Optional[float]]:
        """解析 time_range → (start_ts, end_ts)（epoch 秒；None＝该端不限）。

        支持三种格式（非法抛 ValueError 供 LLM 自纠）：
        - 相对时长：'30m' / '2h' / '3d'（最近该时段）
        - 绝对区间：'起~止'，如 '2026-09-18 10:00~2026-09-18 12:00'；一端可省略
        - 单个时间点：视为从该时刻至今
        """
        text = str(time_range).strip()
        if not text:
            raise ValueError("time_range 不能为空字符串；不需要时间过滤时请省略该参数。")

        # 相对时长
        m = re.fullmatch(r'(\d+)\s*([mhd])', text, re.IGNORECASE)
        if m:
            seconds = int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400}[m.group(2).lower()]
            now = datetime.now().timestamp()
            return now - seconds, now

        # 绝对区间（一端可省略）
        if "~" in text:
            start_text, _, end_text = text.partition("~")
            start_ts = cls._parse_abs_time(start_text.strip())
            end_ts = cls._parse_abs_time(end_text.strip())
            if start_ts is None and end_ts is None:
                raise ValueError(f"time_range 非法: {time_range!r}（区间两端均无法解析为时间）")
            return start_ts, end_ts

        # 单个时间点 → 从该时刻至今
        ts = cls._parse_abs_time(text)
        if ts is not None:
            return ts, None

        raise ValueError(
            f"time_range 非法: {time_range!r}（支持 '30m'/'2h'/'3d' 相对时长，"
            f"或 'YYYY-MM-DD HH:MM~YYYY-MM-DD HH:MM' 绝对区间，一端可省略）"
        )

    @classmethod
    def _parse_abs_time(cls, text: str) -> Optional[float]:
        """解析绝对时间字符串 → epoch 秒；无法解析返回 None（空串亦 None）。"""
        if not text:
            return None
        for fmt in cls._TIME_FORMATS:
            try:
                return datetime.strptime(text, fmt).timestamp()
            except ValueError:
                continue
        return None

    @classmethod
    async def _unknown_session_reply(cls, session_id: str) -> str:
        """会话不存在时的自助纠错回复：列出可用会话供 LLM 重新选择。"""
        from memory.message_repository import get_message_store
        sessions = await get_message_store().list_sessions()
        if not sessions:
            return (
                f"会话 {session_id} 不存在，当前系统中也没有任何会话记录。"
            )
        lines = [
            f"  {s['session_id']}「{s['session_name'] or '未命名'}」({s['message_count']} 条消息)"
            for s in sessions[:10]
        ]
        more = f"\n  ...等共 {len(sessions)} 个会话" if len(sessions) > 10 else ""
        return (
            f"会话 {session_id} 不存在。当前可用的会话：\n" + "\n".join(lines) + more
            + "\n请从上面选择正确的 session_id 重新查询。"
        )
