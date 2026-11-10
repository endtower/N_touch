# -*- coding: utf-8 -*-
"""读取一张本地图片，调用视觉模型回答问题或描述画面。"""

from brain.attachments import resolve_media_file
from brain.attachments.image_input import image_to_data_url
from brain.base.light_agent.lightweight_agent import LightweightAgent
from config.llm_settings import VisionConfig


class VisionAgent(LightweightAgent):
    history_prefix = "vision"

    def _initialize_tool_functions(self) -> dict:
        return {}

    def get_available_tools(self) -> list:
        return []

    def get_system_prompt(self) -> str:
        return (
            "你负责观察图片并用文字回答。若给出具体问题，优先回答问题。"
            "否则描述可见的角色、外形、动作、表情、场景和文字。"
            "对看不清或无法确认的内容说明不确定。"
        )

    def _preprocess(self, query: str, question: str = "") -> list[dict]:
        image = image_to_data_url(
            resolve_media_file(query),
            max_side=VisionConfig.IMAGE_MAX_SIDE,
            quality=VisionConfig.IMAGE_JPEG_QUALITY,
        )
        parts = [{"type": "image_url", "image_url": {"url": image}}]
        if question:
            parts.append({"type": "text", "text": question})
        return parts

    def _should_continue(self, response_message, results: list) -> bool:
        return False

    def _postprocess(self, content: str, extra_info) -> dict:
        return {"content": content.strip() or "视觉模型未返回图片描述。"}

    async def _call_llm(self, messages: list, *, level: str = "vision",
                        system_prompt: str | None = None, **kwargs):
        from utils.llm_client_manager import call_llm

        system_message = {"role": "system", "content": system_prompt or self.get_system_prompt()}
        return await call_llm(level, [system_message, *messages])

    async def run(self, image: str, question: str = "") -> dict:
        result = await self._llm_loop(self._preprocess(image, question), level="vision", max_rounds=1)
        return self._postprocess(result["content"], result["extra_info"])
