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
  ValueError 交由 LLM 自纠；路由发送失败、平台未返回 message_id 直接抛错阻断。
"""

import asyncio
from typing import Any, Dict, List

from utils.logger import get_logger
from adapt import OutboundTarget, OutboundPayload, SessionScope
from adapt.router import get_outbound_router
from adapt.message_segments import text_segment, image_segment
from brain.attachments import resolve_media_file, preflight
from brain.base.common_ways.tool_handlers import ToolHandlers

logger = get_logger('ToolConversation', "INFO")

# 模拟人类打字速度（字/分钟）：决定分段发送时后续条的打字延迟
TYPING_SPEED_CHARS_PER_MINUTE = 150
TYPING_DELAY_PER_CHAR = 60.0 / TYPING_SPEED_CHARS_PER_MINUTE
# 单条打字延迟上限（首条不延时；后续条延时封顶此值）
MAX_TYPING_DELAY = 2.0  # 秒
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
                    "发送纯文字消息。"
                    "就在 message 里用换行分隔：一次调用会拆成多条独立消息，按真人节奏逐条发出。"
                    "长篇的观点论证也可以整段粘贴。但禁止连续发送长篇观点论证，需要有语气词间隔缓冲节奏。"
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
    ) -> str:
        """发送纯文字消息：message 内换行拆成多条独立消息，按真人节奏逐条发出。

        Args:
            session_id: 目标会话标识，格式 "type_id"，如 "private_xxx" / "group_xxx"。
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

        out_segments = [text_segment(ln) for ln in lines]
        scope, target_id = cls._resolve_target(session_id)
        await cls.send_segments_with_typing(
            scope, target_id, out_segments,
            typing_delay_per_char=typing_delay_per_char,
        )
        return cls._render_send_result(len(out_segments))

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
        )
        return cls._render_send_result(1, image=(image or "").strip())

    # ============ 通用发送原语 ============

    @staticmethod
    async def send_segments_with_typing(
        scope,
        target_id: str,
        out_segments: List[Any],
        typing_delay_per_char: float = TYPING_DELAY_PER_CHAR,
    ) -> List[str]:
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
        message_ids: List[str] = []

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

            message_id = await router.send(
                platform="qq",
                target=OutboundTarget(scope=scope, target_id=target_id),
                payload=OutboundPayload(segments=[seg]),
            )
            if not message_id:
                raise RuntimeError(
                    f"send_segments_with_typing 第 {idx + 1}/{len(out_segments)} 条发送失败："
                    "平台未返回 message_id"
                )
            message_ids.append(message_id)

        return message_ids

    # ============ 内部辅助 ============

    @staticmethod
    def _resolve_target(session_id: str):
        """把统一 session_id（type_id）解析为路由 target（scope + target_id）。

        格式非法直接 raise ValueError（不做静默默认路由，防止错发目标）。
        """
        sid = session_id or ""
        if sid.startswith("group_"):
            return SessionScope.GROUP, sid.split("_", 1)[1]
        if sid.startswith("private_"):
            return SessionScope.PRIVATE, sid.split("_", 1)[1]
        raise ValueError(
            f"session_id 格式非法: {session_id!r}（必须为 'group_xxx' / 'private_xxx'）"
        )

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
