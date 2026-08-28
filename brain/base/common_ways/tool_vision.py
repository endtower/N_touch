# -*- coding: utf-8 -*-
"""通用工具：ToolVision（vision 相关工具）

识图 / 表情包管理工具的通用实现，继承 ToolHandlers。

依赖 VisionAgent（识图 agent），位于 brain/base/light_agent/vision_agent.py。

注：call_vision_agent 只负责识图并返回结果；conversation 专有的
"识图记录"（add_recognized_image）由调用方自行处理，不放入通用方法。
list_stickers 的完整实现直接在本类中（从 VisionAgent.list_stickers 迁移），
不再委托 VisionAgent。
"""

import json
from typing import Dict, Any, List

from utils.logger import get_logger
from utils.error_handling import async_handle_errors
from brain.base.common_ways.tool_handlers import ToolHandlers

logger = get_logger('ToolVision', "INFO")


class ToolVision(ToolHandlers):
    """vision 通用工具：识图 + 表情包管理。"""

    # ============ 工具 schema（提示词） ============

    @classmethod
    def get_call_vision_agent_tool_schema(cls) -> Dict[str, Any]:
        """返回 call_vision_agent 工具的 schema。"""
        return {
            "type": "function",
            "function": {
                "name": "call_vision_agent",
                "description": (
                    "呼叫图像识别agent：对用户发送的图片进行识别/保存，生成标题、客观描述、整体感觉，"
                    "图像识别 agent 除识图外，还具备表情包管理能力，可完成表情包的保存、删除、重命名、调整等操作。"
                    "归档目录由源图所在子目录自动推断（无需 LLM 指定）。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "image_filename": {
                            "type": "string",
                            "description": (
                                "图片文件名（不含路径），与消息文本中 [图片:文件名] 的文件名部分一致。"
                                "示例：'039F36C881F4208F4894339B97C5E87C.png' 或 'web_10e3c351ea4d4843b2a863a5e99ce742.png'"
                            )
                        },
                        "context_hint": {
                            "type": "string",
                            "description": (
                                "可选：向识图agent追加的命令，如：'图片中是否有人物'、'图片中人物的性别是男是女'等。"
                            )
                        }
                    },
                    "required": ["image_filename"]
                }
            }
        }

    @classmethod
    def get_list_stickers_tool_schema(cls) -> Dict[str, Any]:
        """返回 list_stickers 工具的 schema。"""
        return {
            "type": "function",
            "function": {
                "name": "list_stickers",
                "description": (
                    "查询已保存的表情包库（media/stickers/ 目录）。"
                    "可选传入关键词过滤（匹配文件名 / 标题 / 描述 / 情绪）。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keyword": {
                            "type": "string",
                            "description": "【可选】关键词，如'猫'、'生气'、'开心'等"
                        }
                    },
                    "required": []
                }
            }
        }

    # ============ 通用执行方法 ============

    @classmethod
    async def call_vision_agent(cls, image_filename: str, context_hint: str = None) -> Dict[str, Any]:
        """识图：调用 VisionAgent 识别图片并返回结果。

        只负责识图并返回结果 dict；conversation 专有的"识图记录"由调用方自行处理。

        Args:
            image_filename: 图片文件名（与消息中 [图片:xxx] 的 xxx 一致）。
            context_hint: 可选附加提示，辅助识图模型理解。

        Returns:
            识图结果 dict，仅含 content（str）：{ "content": "..." }。
        """
        from brain.base.light_agent.vision_agent import VisionAgent

        hint_str = (context_hint or "").strip()
        logger.info(
            f"[recognize_image] LLM 请求识图: {image_filename}, context_hint={hint_str!r}"
            if hint_str else
            f"[recognize_image] LLM 请求识图: {image_filename}"
        )

        try:
            result = await VisionAgent().run(
                image_filename,
                additional_hint=hint_str or None,
            )
        except Exception as e:
            logger.error(f"[recognize_image] 调用失败: {e}", exc_info=True)
            return {"content": f"识图失败：{e}"}

        return {"content": result.get("content") or "vision agent 未返回任何信息。"}

    @classmethod
    @async_handle_errors()
    async def list_stickers(cls, keyword: str = "") -> str:
        """查询已保存的表情包库（memory/llm_save/stickers/）。

        完整实现（从 VisionAgent.list_stickers 迁移）：扫描 LLM 自存表情包目录，
        按关键词过滤（文件名 / 标题 / 描述 / 情绪任一命中），按 saved_at 倒序，
        拼装成可直接喂给 LLM 的文案 str 返回。
        """
        from brain.base.light_agent.vision_agent import LLM_SAVE_ROOT, LLM_SAVE_STICKER_SUBDIR

        kw = (keyword or "").strip().lower()
        sticker_root = LLM_SAVE_ROOT / LLM_SAVE_STICKER_SUBDIR
        if not sticker_root.exists():
            return "表情包库为空。你可以找主人要表情包。"

        stickers: List[Dict[str, Any]] = []
        for img_path in sticker_root.iterdir():
            if not img_path.is_file():
                continue
            if img_path.suffix.lower() not in (".jpg", ".jpeg", ".png", ".gif"):
                continue

            meta_path = img_path.with_suffix(".json")
            if not meta_path.exists():
                continue

            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}

            item = {
                "filename": img_path.name,
                "sticker_title": meta.get("sticker_title") or meta.get("title", ""),
                "description": meta.get("description", ""),
                "mood": meta.get("mood", ""),
                "saved_at": meta.get("saved_at", ""),
            }

            if kw:
                haystack = " ".join([
                    item["filename"],
                    item["sticker_title"],
                    item["description"],
                    item["mood"],
                ]).lower()
                if kw not in haystack:
                    continue

            stickers.append(item)

        stickers.sort(key=lambda x: x.get("saved_at") or "", reverse=True)

        if not stickers:
            suffix_msg = f"（关键词：{keyword}）" if kw else ""
            return f"未找到匹配的表情包{suffix_msg}。你可以找主人要表情包。"

        lines = [f"已找到 {len(stickers)} 个表情包："]
        for idx, s in enumerate(stickers, 1):
            title = s["sticker_title"] or s["filename"]
            tail = " | ".join(x for x in (s["description"], s["mood"]) if x)
            line = f"{idx}. [表情:{s['filename']}] {title}"
            if tail:
                line += f" — {tail}"
            lines.append(line)

        return "\n".join(lines) + "\n你可以找主人要表情包。"
