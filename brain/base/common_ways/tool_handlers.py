# -*- coding: utf-8 -*-
"""通用工具基类：ToolHandlers

把 conversation / social_chat 共用的工具能力抽到 brain/base/common_ways，
工具描述（schema）与具体执行逻辑对应存放。

通用方法要求外部传入目标 session_id（格式 "type_id"，如 "private_xxx" / "group_xxx"），
由调用方（各 ToolManager）在内部自动注入，LLM 无需感知 session_id。

子类：
- ToolVision：vision 相关工具（识图 / 表情包管理）
"""

import asyncio
import time
from typing import Dict, Any, List

from utils.logger import get_logger
from adapt import OutboundTarget, OutboundPayload, SessionScope
from adapt.router import get_outbound_router
from adapt.message_segments import text_segment, image_segment
from brain.attachments import resolve_media_file, preflight

logger = get_logger('ToolHandlers', "INFO")

# 段与段之间的"分条节奏"延迟（真人连发短句的节奏感下限）
INTER_SEGMENT_GAP = 1.5  # 秒
# 单段打字延迟上限
MAX_SEGMENT_TYPING = 7.0  # 秒


class ToolHandlers:
    """通用工具基类：send_message / wait_and_observe 及其 schema。"""

    # ============ 工具 schema（提示词） ============

    @classmethod
    def get_send_message_tool_schema(cls) -> Dict[str, Any]:
        """返回 send_message 工具的 schema（通用部分：仅 segments）。

        注：conversation 专有的 nature（发言性质，用 SelfActionAct 枚举）由
        conversation 模块在 get_tools() 中自行扩展，不放入通用 schema。
        """
        return {
            "type": "function",
            "function": {
                "name": "send_message",
                "description": (
                    "向当前会话发送消息。segments 是段数组，按顺序拼接显示。\n"
                    "每段是单 key 字典，key 即段类型：\n"
                    "  {text: '...'}   纯文字\n"
                    "  {img:  '...'}   图片文件名（从消息文本 [图片:xxx] 里取，\n"
                    "示例（文字+图片+文字穿插）：\n"
                    "  segments=[{text:'看这里 '},{img:'30FBB5FBD24B8D4B2660C3E3C0515F76.gif'},{text:' 然后告诉我'}]"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "segments": {
                            "type": "array",
                            "maxItems": 9,
                            "description": "消息段数组，按顺序拼接显示。最多 9 段（QQ 移动端单消息图片上限）。",
                            "items": {
                                "type": "object",
                                "description": "单 key 段，key 即段类型，值为该类型的内容。",
                                "properties": {
                                    "text": {"type": "string", "description": "文字内容"},
                                    "img":  {"type": "string", "description": "图片文件名（不含路径），必须与消息中 [图片:xxx] 的 xxx 一致。"}
                                },
                                "additionalProperties": False,
                                "minProperties": 1,
                                "maxProperties": 1
                            }
                        }
                    },
                    "required": ["segments"]
                }
            }
        }

    @classmethod
    def get_wait_and_observe_tool_schema(cls) -> Dict[str, Any]:
        """返回 wait_and_observe 工具的 schema（通用部分）。"""
        return {
            "type": "function",
            "function": {
                "name": "wait_and_observe",
                "description": "等待一段时间，等待对方消息的回复。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "seconds": {
                            "type": "number",
                            "description": "等待的秒数",
                            "minimum": 10.0,
                            "maximum": 30.0
                        },
                    },
                    "required": ["seconds"]
                }
            }
        }

    # ============ 内部辅助 ============

    @staticmethod
    def _resolve_target(session_id: str):
        """把统一 session_id（type_id）解析为路由 target（scope + target_id）。"""
        if session_id.startswith("group_"):
            return SessionScope.GROUP, session_id.split("_", 1)[1]
        if session_id.startswith("private_"):
            return SessionScope.PRIVATE, session_id.split("_", 1)[1]
        return SessionScope.GROUP, session_id

    @staticmethod
    def _build_out_segments(segments: List[Any]) -> List[Any]:
        """把 LLM 传入的 segments 转为内部 MessageSegment。

        非法 / 不存在的图片立即返回错误信息（不应静默忽略；LLM 应该被告知出错）。
        未知 key 静默忽略。

        Returns:
            (out_segments, error)：error 非空表示解析失败，直接返回该错误信息。
        """
        out_segments: list = []
        for seg in segments or []:
            if not isinstance(seg, dict):
                continue
            if "text" in seg:
                txt = str(seg["text"] or "")
                if not txt:
                    continue
                out_segments.append(text_segment(txt))
            elif "img" in seg:
                filename = str(seg["img"] or "").strip()
                if not filename:
                    continue
                try:
                    real_path = resolve_media_file(filename)
                except ValueError as e:
                    return [], f"发送失败：图片 {filename} 格式非法"
                except FileNotFoundError:
                    return [], f"发送失败：图片 {filename} 不存在或已被清理"
                except PermissionError as e:
                    return [], f"发送失败：图片 {filename} 不在允许目录内"

                check = preflight(real_path)
                if not check["ok"]:
                    return [], f"发送失败：{check['reason']}"

                out_segments.append(image_segment(file=check["path"]))
        return out_segments, ""

    # ============ 通用执行方法 ============

    @classmethod
    async def send_message(
        cls,
        session_id: str,
        segments: List[Any],
        typing_delay_per_char: float = 0.6,
        on_segment_sent=None,
    ) -> str:
        """通用发送消息：把 segments 拆成独立消息逐条发送到目标会话。

        多段（segments 数组）会被拆成【独立的多条消息】按顺序逐条发送，
        每条消息发送前有基于该段内容的打字延迟；段与段之间有固定的"分条节奏"延迟。

        Args:
            session_id: 目标会话标识，格式 "type_id"，如 "private_xxx" / "group_xxx"。
            segments: LLM 传入的段列表。每项是单 key 字典（text 或 img）。
            typing_delay_per_char: 每字符打字延迟（秒），由调用方按会话类型注入。
            on_segment_sent: 可选回调，每段发送成功后调用（供调用方做计数等附加逻辑）。

        Returns:
            发送结果字符串（成功 / 部分成功 / 失败）。
        """
        import datetime
        current_time = datetime.datetime.now().strftime("%H:%M:%S")

        out_segments, err = cls._build_out_segments(segments)
        if err:
            return err
        if not out_segments:
            return f"发送失败，消息为空，当前时间：{current_time}"

        scope, target_id = cls._resolve_target(session_id)

        try:
            router = await get_outbound_router()
        except Exception as e:
            logger.error(f"send_message 获取 OutboundRouter 失败: {e}")
            return f"发送失败，当前时间：{current_time}，错误：{e}"

        sent_count = 0
        fail_messages: list = []

        for idx, seg in enumerate(out_segments):
            # 该段的打字延迟
            call_start = time.time()
            if seg.type == "text":
                per_seg_typing = len(seg.to_text()) * typing_delay_per_char
            elif seg.type == "image":
                per_seg_typing = 1.0
            else:
                per_seg_typing = 0.0
            per_seg_typing = min(per_seg_typing, MAX_SEGMENT_TYPING)
            elapsed_in_method = time.time() - call_start
            remaining = max(0.0, per_seg_typing - elapsed_in_method)
            if remaining > 0:
                logger.info(
                    f"send_message 段 {idx + 1}/{len(out_segments)} 打字延迟：等待 {remaining:.2f} 秒"
                    f"（type={seg.type}, 内容长度={len(seg.to_text())}）"
                )
                await asyncio.sleep(remaining)

            # 单段路由发送（payload 里只放当前段）
            try:
                message_id = await router.send(
                    platform="qq",
                    target=OutboundTarget(scope=scope, target_id=target_id),
                    payload=OutboundPayload(segments=[seg]),
                )
            except Exception as e:
                logger.error(f"send_message 段 {idx + 1} 路由失败: {e}")
                fail_messages.append(f"段 {idx + 1} 失败：{e}")
                continue

            if not message_id:
                fail_messages.append(f"段 {idx + 1} 发送失败")
                continue

            sent_count += 1
            if on_segment_sent is not None:
                on_segment_sent()

            # 段间节奏延迟（最后一段不需要再等）
            if idx < len(out_segments) - 1:
                logger.debug(
                    f"send_message 段 {idx + 1}/{len(out_segments)} 已发送，"
                    f"等待 {INTER_SEGMENT_GAP} 秒进入下一段"
                )
                await asyncio.sleep(INTER_SEGMENT_GAP)

        if sent_count == 0:
            reason = "；".join(fail_messages) or "未知错误"
            return f"发送失败，当前时间：{current_time}，错误：{reason}"

        if fail_messages:
            return (
                f"部分发送成功 {sent_count}/{len(out_segments)} 段，当前时间：{current_time}；"
                f"失败段：{'；'.join(fail_messages)}"
            )
        return (
            f"发送成功 {sent_count} 段，当前时间：{current_time}"
            if sent_count > 1 else
            f"发送成功，当前时间：{current_time}"
        )

    @classmethod
    async def wait_and_observe(
        cls,
        session_id: str,
        seconds: float,
        consumer_id: str,
    ) -> str:
        """通用等待并监听：等待目标会话的新消息，超时或检测到新消息即返回。

        Args:
            session_id: 目标会话标识，格式 "type_id"，如 "private_xxx" / "group_xxx"。
            seconds: 最长等待秒数。
            consumer_id: 消费者标识（如 "conversation" / "social_chat"），用于游标读取。

        Returns:
            结果字符串（检测到新消息 / 等待完成）。
        """
        from memory.session_history import (
            get_global_session_history,
            FetchOptions,
            MessageFormatOptions,
        )

        history = get_global_session_history()
        start_time = time.time()
        elapsed = 0

        while elapsed < seconds:
            wait_time = min(1.0, seconds - elapsed)
            await asyncio.sleep(wait_time)
            elapsed = time.time() - start_time
            # 监听场景：advance_cursor=False，只读不推进游标，消息保持未读供后续消费。
            result = await history.fetch_unread(
                session_id,
                "",
                consumer_id=consumer_id,
                options=FetchOptions(
                    message_format=MessageFormatOptions(exclude_system=True, show_timestamp=True)
                ),
                advance_cursor=False,
            )
            if result.has_new:
                import datetime
                current_time = datetime.datetime.now().strftime("%H:%M:%S")
                result_str = f"检测到用户输入，中断等待，总共等待 {int(elapsed)} 秒，当前时间：{current_time}"
                logger.info(result_str)
                return result_str

        import datetime
        current_time = datetime.datetime.now().strftime("%H:%M:%S")
        result_str = f"等待观察完成，总共等待 {int(elapsed)} 秒，当前时间：{current_time}"
        logger.info(result_str)

        return result_str
