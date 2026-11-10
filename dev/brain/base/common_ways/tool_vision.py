# -*- coding: utf-8 -*-
"""会话模型按需查看本地图片的只读工具。"""

from utils.logger import get_logger

logger = get_logger("ToolVision", "INFO")


class ToolVision:
    @staticmethod
    def get_inspect_image_tool_schema() -> dict:
        return {
            "type": "function",
            "function": {
                "name": "inspect_image",
                "description": (
                    "根据本地文件名查看一张图片并返回文字描述。"
                    "当前输入已经附上原图时可直接看图；只有需要补看图片或细节时才调用。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "image": {
                            "type": "string",
                            "description": "图片文件名，取聊天或搜索结果中方括号里的文件名",
                        },
                        "question": {
                            "type": "string",
                            "description": "可选的具体问题，例如‘角色的手势是什么？’",
                        },
                    },
                    "required": ["image"],
                },
            },
        }

    @staticmethod
    async def inspect_image(image: str, question: str = "") -> str:
        if not isinstance(image, str) or not isinstance(question, str):
            raise TypeError("识图工具的参数必须是字符串。")
        image = image.strip()
        if not image:
            return "识图失败：请提供图片文件名。"

        from brain.base.light_agent.vision_agent import VisionAgent

        try:
            result = await VisionAgent().run(image, question=question.strip())
        except FileNotFoundError:
            return f"识图失败：本地找不到图片 {image}，请核对文件名。"
        except (ValueError, PermissionError):
            return "识图失败：请提供允许访问的本地图片文件名。"
        except Exception:
            logger.exception("识图调用失败：%s", image)
            return "识图失败：无法读取图片或获取视觉模型结果。"
        return result["content"]
