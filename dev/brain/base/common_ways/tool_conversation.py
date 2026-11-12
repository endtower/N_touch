# -*- coding: utf-8 -*-
"""通用工具：ToolConversation（会话发送类工具）

send_message / send_image 的 schema 与执行统一收口在本模块，
conversation 与 social_chat 各自的 ToolManager 只做目标注入与专有扩展（nature）。

设计要点（替换旧 segments 段数组方案）：
- 文字与图片拆成两个独立工具：send_message 只发纯文字，send_image 只发图片，
  参数扁平化，LLM 不再构造段数组。
- message 内换行即分条：一次调用把多句拆成多条独立消息，按真人节奏逐条发出
  （首条立即发送，后续条按打字速度模拟延迟、上限封顶），
  省去"每条等 tool 返回再发下一条"的多轮调用。
- fail-fast：参数问题（空消息 / 文件名非法 / 图片不存在 / preflight 失败）raise
  ValueError 交由 LLM 自纠；即时通道无 message_id 抛错，桥接回执按实际阶段返回，结果不明不重发。
"""

import asyncio
from typing import Any, Callable, Dict, List, Optional

from utils.logger import get_logger
from adapt import OutboundTarget, OutboundPayload, SessionScope
from adapt.router import get_outbound_router
from adapt.bridge.contract import SendReceipt
from adapt.session_identity import parse_session_id
from adapt.message_segments import text_segment, image_segment
from brain.attachments import resolve_media_file, preflight
from brain.base.common_ways.tool_handlers import ToolHandlers

logger = get_logger('ToolConversation', "INFO")

# 模拟人类打字速度（字/分钟）：决定分段发送时后续条的打字延迟
TYPING_SPEED_CHARS_PER_MINUTE = 150
TYPING_DELAY_PER_CHAR = 60.0 / TYPING_SPEED_CHARS_PER_MINUTE
# 单条打字延迟上限（首条不延时；后续条延时封顶此值）
MAX_TYPING_DELAY = 2.0  # 秒
SEND_MESSAGE_RULE = "每个 LLM 执行轮次只能调用一次文字发送工具；message 用换行分段，一次最多发送 3 条，超出部分不会发送。"
# 图片发送的固定打字延迟
IMAGE_TYPING_DELAY = 1.0  # 秒


class ToolConversation(ToolHandlers):
    """会话发送类通用工具：send_message / send_image 的 schema、执行与发送原语。"""

    # ============ 工具 schema（提示词） ============

    @classmethod
    def get_send_message_tool_schema(cls) -> Dict[str, Any]:
        """返回 send_message 工具的 schema（通用部分：仅 message）。

        注：conversation 专有的 nature（发言性质，用 SelfActionAct 枚举）由
        conversation / social_chat 各自在 get_tools() 中自行扩展，不放入通用 schema。
        """
        return {
            "type": "function",
            "function": {
                "name": "send_message",
                "description": (
                    "发送纯文字消息。" + SEND_MESSAGE_RULE +
                    "就在 message 里用换行分隔：一次调用会拆成多条独立消息，按真人节奏逐条发出。"
                    "长篇的观点论证也可以整段粘贴，发完后避免连续追加长篇。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message": {
                            "type": "string",
                            "description": (
                                "要发送的文字内容。严禁用空格代替标点断句"
                                "（正确：'出金爽三秒，扣款疼一个月'；错误：'出金爽三秒 扣款疼一个月'）。"
                                "连发多条短句时用换行分隔（一次调用拆成多条消息逐条发出），"
                                "短句句末不加标点，需要强调时可单独发送标点字符（如'？''！'）表达情绪；"
                                "整段的观点论证保持完整，作为一整条发出，内部正常使用标点。"
                                "禁止描写身体动作。禁止为空。"
                            ),
                        }
                    },
                    "required": ["message"],
                },
            },
        }

    @classmethod
    def get_send_image_tool_schema(cls) -> Dict[str, Any]:
        """返回 send_image 工具的 schema（仅 image）。"""
        return {
            "type": "function",
            "function": {
                "name": "send_image",
                "description": "发送图片。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "image": {
                            "type": "string",
                            "description": (
                                "图片文件名（不含路径），必须与消息文本中 [图片:xxx] 的 xxx 一致。"
                            ),
                        },
                    },
                    "required": ["image"],
                },
            },
        }

    # ============ 通用执行方法 ============

    @classmethod
    async def send_message(
        cls,
        session_id: str,
        message: str,
        typing_delay_per_char: float = TYPING_DELAY_PER_CHAR,
        on_sent: Optional[Callable[[str], None]] = None,
        before_send: Optional[Callable[[], None]] = None,
    ) -> str:
        """发送纯文字消息：message 内换行拆成多条独立消息，按真人节奏逐条发出。

        Args:
            session_id: 目标会话标识，必须逐字照抄通讯录给出的完整 id（含平台前缀），
                如 "wechat_private_10003" / "qq_private_1234567890" / "qq_group_198431299"。
            message: 要发送的文字内容，换行分隔多条短句；全空 raise ValueError。
            typing_delay_per_char: 每字符打字延迟（秒），由调用方按会话类型注入。

        Returns:
            发送结果字符串。

        Raises:
            ValueError: message 为空 / session_id 格式非法（LLM 可自纠的参数错误）。
        """
        lines = [
            ln.strip()
            for ln in (message or "").replace("\\n", "\n").split("\n")
            if ln.strip()
        ]  # 字面 "\n"（LLM 偶发的转义错误）归一化为真实换行后再拆条
        if not lines:
            raise ValueError("message 为空，禁止发送空消息")

        scope, target_id = cls._resolve_target(session_id)
        from memory.message_repository import get_message_store
        pressure = await get_message_store().get_speech_pressure(session_id)
        count = pressure["characters"]
        limited = not pressure["user_replied"] and count >= 150
        max_parts = (1 if count >= 300 else 2) if limited else 3
        omitted = lines[max_parts:]
        out_segments = [text_segment(ln) for ln in lines[:max_parts]]
        receipts = await cls.send_segments_with_typing(
            scope, target_id, out_segments,
            typing_delay_per_char=typing_delay_per_char,
            session_id=session_id,
            before_send=before_send,
        )
        bridge_receipts = [receipt for receipt in (receipts or []) if isinstance(receipt, SendReceipt)]
        if on_sent is not None and (not bridge_receipts or all(r.status == "observed" for r in bridge_receipts)):
            on_sent("\n".join(lines[:max_parts]))
        result = cls._render_bridge_result(bridge_receipts) if bridge_receipts else cls._render_send_result(len(out_segments))
        # 发送本次长篇后立即告知下一轮约束；正常发送不添加提示。
        pressure = await get_message_store().get_speech_pressure(session_id)
        if not pressure["user_replied"] and pressure["characters"] >= 150:
            count = pressure["characters"]
            next_parts, soft_chars = (1, 60) if count >= 300 else (2, 120)
            result += (
                f"\n【发言量提醒】最近60秒已发送{count}个非空白字符。"
                f"若下一轮没有用户新回复，最多发送{next_parts}条，"
                f"总长度尽量不超过{soft_chars}字；没有必要的新内容就停止发送。"
            )
        if omitted:
            result += (
                f"\n【警告】本次最多允许{max_parts}条，超出的{len(omitted)}条未发送。"
                "不得在本轮再次调用发送工具补发。未发送内容：\n"
                + "\n".join(f"{i}. {line}" for i, line in enumerate(omitted, max_parts + 1))
            )
        return result

    @classmethod
    async def send_image(
        cls,
        session_id: str,
        image: str,
        typing_delay_per_char: float = TYPING_DELAY_PER_CHAR,
    ) -> str:
        """发送图片（单张；配文用 send_message 另发）。

        Args:
            session_id: 目标会话标识，格式 "type_id"。
            image: 图片文件名（不含路径），与消息文本 [图片:xxx] 的 xxx 一致。
            typing_delay_per_char: 每字符打字延迟（秒）。

        Returns:
            发送结果字符串。

        Raises:
            ValueError: 文件名为空 / 图片格式非法 / 不存在 / 不在允许目录 /
                preflight 失败（LLM 可自纠的参数错误）。
        """
        image_seg = cls._build_image_segment(image)

        scope, target_id = cls._resolve_target(session_id)
        await cls.send_segments_with_typing(
            scope, target_id, [image_seg],
            typing_delay_per_char=typing_delay_per_char,
            session_id=session_id,
        )
        return cls._render_send_result(1, image=(image or "").strip())

    # ============ 通用发送原语 ============

    @staticmethod
    async def send_segments_with_typing(
        scope,
        target_id: str,
        out_segments: List[Any],
        typing_delay_per_char: float = TYPING_DELAY_PER_CHAR,
        session_id: str | None = None,
        before_send: Optional[Callable[[], None]] = None,
    ) -> List[str | SendReceipt]:
        """带打字节奏逐条发送的机械原语（fail-fast：任何条发送失败直接抛错阻断）。

        发送节奏：第一条立即发送（LLM 生成耗时视作拟稿时间）；后续条按
        打字速度模拟真人输入（文本按字数 × typing_delay_per_char，图片固定
        IMAGE_TYPING_DELAY），单条延迟上限 MAX_TYPING_DELAY。
        只负责"逐条打字延迟 + 路由发送 + 收集平台 message_id"，不含错误文案包装——
        错误语义由调用方决定。参数问题 raise ValueError；路由失败 / 平台未返回
        message_id 直接抛错阻断（符合 user_rules：发送链路不吞错、不静默降级）。

        Args:
            scope: SessionScope（GROUP / PRIVATE）。
            target_id: 目标 ID（QQ 号 / 群号）。
            out_segments: 已解析的 MessageSegment 列表（段解析与图片 preflight 由调用方完成）。
            typing_delay_per_char: 每字符打字延迟（秒）。

        Returns:
            各条平台 message_id 列表（按发送顺序，与 out_segments 一一对应）。

        Raises:
            RuntimeError: 平台未返回 message_id（基础设施故障，崩溃阻断）。
        """
        if not out_segments:
            raise ValueError("send_segments_with_typing 没有任何待发送条")

        router = await get_outbound_router()
        if before_send is not None:
            before_send()
        message_ids: List[str | SendReceipt] = []

        for idx, seg in enumerate(out_segments):
            # 第一条不延时立即发送；后续条模拟真人打字节奏（上限封顶 MAX_TYPING_DELAY）
            if idx == 0:
                per_seg_typing = 0.0
            elif seg.type == "text":
                per_seg_typing = min(len(seg.to_text()) * typing_delay_per_char, MAX_TYPING_DELAY)
            elif seg.type == "image":
                per_seg_typing = min(IMAGE_TYPING_DELAY, MAX_TYPING_DELAY)
            else:
                per_seg_typing = 0.0
            if per_seg_typing > 0:
                logger.info(
                    f"send_segments_with_typing 第 {idx + 1}/{len(out_segments)} 条 "
                    f"打字延迟：等待 {per_seg_typing:.2f} 秒（type={seg.type}）"
                )
                await asyncio.sleep(per_seg_typing)

            message_id = await router.send_to_session(
                session_id or f"{scope.value}_{target_id}", OutboundPayload(segments=[seg]))
            if not message_id:
                raise RuntimeError(
                    f"send_segments_with_typing 第 {idx + 1}/{len(out_segments)} 条发送失败："
                    "平台未返回 message_id"
                )
            message_ids.append(message_id)
            if isinstance(message_id, SendReceipt) and message_id.status in ("unknown", "prepared", "rejected", "failed"):
                break  # Do not submit the remaining parts after an uncertain or failed send.

        return message_ids

    # ============ 内部辅助 ============

    @staticmethod
    def _render_bridge_result(receipts: list[SendReceipt]) -> str:
        labels = {"queued": "已受理，等待执行", "actioned": "已执行发送，尚未在界面确认",
                  "observed": "已在界面观察到发出，未证明对方收到", "unknown": "发送结果不明确",
                  "prepared": "请求已保存，尚未确认受理", "rejected": "请求被拒绝", "failed": "发送失败"}
        return "；".join(labels.get(receipt.status, "发送结果不明确") for receipt in receipts) + "。不要重复发送或补发；由用户核对平台状态。"

    @staticmethod
    def _resolve_target(session_id: str):
        """把统一 session_id（type_id）解析为路由 target（scope + target_id）。

        格式非法直接 raise ValueError（不做静默默认路由，防止错发目标）。
        """
        parsed = parse_session_id(session_id or "")
        if parsed.scope not in ("private", "group"):
            raise ValueError("只允许向已知的私聊或群聊发送")
        return SessionScope(parsed.scope), parsed.identity

    @staticmethod
    def _build_image_segment(image: str):
        """把图片文件名解析为 image 段（resolve + preflight）。

        Raises:
            ValueError: 文件名为空 / 格式非法 / 不存在 / 不在允许目录 / preflight 失败
                （LLM 可自纠的参数错误；resolve_media_file 的 FileNotFoundError /
                PermissionError 在此统一转为 ValueError）。
        """
        filename = (image or "").strip()
        if not filename:
            raise ValueError("image 文件名为空")

        try:
            real_path = resolve_media_file(filename)
        except FileNotFoundError:
            raise ValueError(f"图片 {filename} 不存在或已被清理")
        except PermissionError:
            raise ValueError(f"图片 {filename} 不在允许目录内")

        check = preflight(real_path)
        if not check["ok"]:
            raise ValueError(f"发送失败：图片 {filename} {check['reason']}")

        return image_segment(file=check["path"])

    @staticmethod
    def _render_send_result(count: int, image: str = "") -> str:
        """渲染发送结果文案：count>1 注明条数；image 非空注明图片文件名。"""
        import datetime
        current_time = datetime.datetime.now().strftime("%H:%M:%S")
        parts: List[str] = []
        if image:
            parts.append(f"图片 {image}")
        if count > 1:
            parts.append(f"共 {count} 条")
        detail = f"（{'，'.join(parts)}）" if parts else ""
        return f"发送成功{detail}，当前时间：{current_time}"
