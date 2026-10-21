# -*- coding: utf-8 -*-
"""识图Agent —— 输入图片 → 调用视觉模型生成标题与描述 → 以"标题"为名转存图片到 media/vision_results/

按 `lightweight_agent.LightweightAgent` 的五段式骨架重写整理顺序：
  1. 前处理 `_preprocess`   —— 解析源图、转 base64、构造 LLM 首条 user 消息、注入源图上下文
  2. LLM 循环 `_llm_loop`   —— 复用父类骨架（调视觉模型 → 判断是否继续 → 执行工具并回灌）
  3. 是否继续 `_should_continue` —— 单轮语义：有工具调用就继续执行，否则终止
  4. 工具处理 `_process_tool`  —— 复用父类实现
  5. 后处理 `_postprocess`   —— LLM 退出循环后，从工具结果中整理最终返回文案

关键失败点（模块实例未运行、获取失败）按项目约定直接崩溃阻断，禁用 try-ex 吞错蔓延。
"""

import sys
import json
import shutil
import base64
from typing import List, Dict, Any, Optional
from datetime import datetime
from pathlib import Path

# 当前文件位于: N_Touch/brain/base/light_agent/vision_agent.py
# parents[0]=light_agent, [1]=base, [2]=brain, [3]=N_Touch
from paths import data_root as _data_root
_PROJECT_ROOT = _data_root()

from brain.base.light_agent.lightweight_agent import LightweightAgent
from utils.logger import get_logger
from config.llm_settings import VisionConfig

logger = get_logger(__name__, "DEBUG")


# ===================== 媒体目录常量 =====================
# 系统侧媒体根：<项目根>/memory/chat_history/media/
MEDIA_ROOT = _PROJECT_ROOT / "memory" / "chat_history" / "media"
STICKER_SUBDIR = "stickers"            # 系统接收到的原始表情包归档子目录（含日期子目录）
IMAGES_SUBDIR = "images"               # 系统接收到的原始图片归档子目录（含日期子目录）
VISION_RESULT_SUBDIR = "vision_results"  # 识图结果存放子目录（图片与同名 .json 都在这里）

# LLM 侧媒体根：<项目根>/memory/llm_save/{stickers,images}/
LLM_SAVE_ROOT = _PROJECT_ROOT / "memory" / "llm_save"
LLM_SAVE_STICKER_SUBDIR = "stickers"
LLM_SAVE_IMAGE_SUBDIR = "images"


class VisionAgent(LightweightAgent):
    """识图Agent —— 输入图片 → 生成标题与描述 → 以标题为新文件名转存到目标文件夹。

    继承 `LightweightAgent`，按五段式骨架组织：
    - 前处理 `_preprocess`：解析源图、转 data_url、构造 user 消息、注入 _current_source
    - LLM 循环 `_llm_loop`：复用父类，由 `_call_llm` 覆盖走 vision 等级模型
    - 后处理 `_postprocess`：从循环产出的工具结果整理最终文案

    事件链上下文保留：history_prefix="vision"，经父类 _llm_loop 落盘
    event_chains/{chain_id}/vision_{HHMMSS}.json（前端 /context 以 kind=vision 展示）。

    提供给 LLM 的工具（schema 由 get_available_tools() 暴露）：
      * save_image          ：保存普通图片（title / description / mood 必填；源图锁定 _current_source）
      * save_sticker        ：保存表情包（sticker_title / description / mood 必填；源图锁定 _current_source）
      * adjust_sticker      ：修改最近保存/调整过的表情包及其同名 .json（sticker_title / description / mood 必填）
      * delete_sticker      ：删除最近保存/调整过的表情包及其同名 .json（无参）

    表情包库的列举 / 检索不在此处：统一走上层 ToolVision.vision_agent 的
      「向量比对」（向量检索），本 agent 只负责单张图片的识图与保存。
    """

    history_prefix: str = "vision"

    # ===================== 1. 前处理 =====================
    def _preprocess(self, query: str) -> Any:
        """前处理：LLM 进入正式循环之前，对输入信息进行初次处理。

        这里把识图入口的入参（image_path / additional_hint）解析、归一化，
        构造出视觉模型可用的首条 user 消息（含图片 data_url），并注入源图上下文。

        Returns:
            构造好的 user 消息 content（List[Dict]，image_url + 可选 text）。
        """
        image_path = getattr(self, "_input_image_path", query)
        additional_hint = getattr(self, "_input_hint", None)

        # 1. 解析图片路径（绝对/相对/裸文件名/扩展名兜底）
        path = self._resolve_input_path(image_path)

        logger.info(f"[VisionAgent] 开始识别: {path}")

        # 2. 图片 → base64 data_url
        data_url = self._image_to_data_url(
            str(path),
            max_side=VisionConfig.IMAGE_MAX_SIDE,
            quality=VisionConfig.IMAGE_JPEG_QUALITY,
        )

        # 3. 构造 user 消息（供父类 _llm_loop 作为首条 user 消息）
        user_content: List[Dict[str, Any]] = [
            {"type": "image_url", "image_url": {"url": data_url}}
        ]
        if additional_hint:
            user_content.append({"type": "text", "text": additional_hint})

        # 4. 注入源图上下文；save_image / save_sticker 不必让 LLM 重新声明源文件名
        self._current_source = path

        return user_content

    # ===================== 3. 是否继续 =====================
    def _should_continue(self, response_message, results: List[Dict[str, Any]]) -> bool:
        """是否继续：单轮语义。

        - 本轮 LLM 返回了工具调用 → 返回 True，让父类执行工具（落盘工具执行后即收尾）。
        - 本轮无工具调用（LLM 已收尾）→ 返回 False，终止循环。
        """
        return bool(response_message.tool_calls)

    # ===================== 5. 后处理 =====================
    def _postprocess(self, content: str, extra_info: Any) -> Dict[str, Any]:
        """后处理：LLM 退出循环之后，整理最终返回值。

        从循环产出的 extra_info.tools_called 中取第一个工具的 result，
        拼装为最终 content 返回。
        """
        tools_called = (extra_info or {}).get("tools_called") or []

        if not tools_called:
            return {"content": "vision agent 未调用任何工具。"}

        first = tools_called[0]
        tool_result = (first.get("result") or {}) if isinstance(first.get("result"), dict) else {}

        if tool_result.get("status") != "completed":
            return {
                "content": tool_result.get("summary")
                or tool_result.get("message", "")
                or "识图失败",
            }

        return {
            "content": tool_result.get("summary") or content or "vision agent 未返回任何信息。",
        }

    # ===================== 统一入口（重写以适配双入参，内部仍走三段式骨架） =====================
    async def run(self, image_path: str, additional_hint: Optional[str] = None) -> Dict[str, Any]:
        """识图入口：前处理 → LLM 循环 → 后处理。

        Args:
            image_path: 图片绝对路径，或相对 media/ 的路径。
            additional_hint: 可选附加提示（透传给视觉模型）。

        Returns:
            {"content": "...", "extra_info": ...}。
        """
        self._input_image_path = image_path
        self._input_hint = additional_hint
        try:
            processed_input = self._preprocess(image_path)
            loop_result = await self._llm_loop(processed_input, level="vision")
            content = loop_result.get("content", "")
            extra_info = loop_result.get("extra_info")
            return self._postprocess(content, extra_info)
        finally:
            self._current_source = None
            self._input_image_path = None
            self._input_hint = None

    # ===================== 视觉模型专用 LLM 调用（覆盖父类默认桥接） =====================
    async def _call_llm(self, messages: list, *, level: str = "vision",
                        tool_choice: Optional[str] = None,
                        system_prompt: Optional[str] = None, **kwargs):
        """识图专用 LLM 调用：走 vision 等级（LLMSettings.VISION，当前 = deepseek-flash）。

        统一经 utils.llm_client_manager 的异步桥接（不再自建同步 openai 客户端）：
        接入点、思考开关、token 统计与全系统其它 LLM 调用保持一致；vision 位与
        flash 同走 DeepSeek 官方接入点（deepseek-flash 原生支持图像理解）。
        """
        from utils.llm_client_manager import call_llm

        sp = system_prompt if system_prompt is not None else self.get_system_prompt()
        full_messages = [{"role": "system", "content": sp}] + messages

        return await call_llm(
            level,
            full_messages,
            tools=self.get_available_tools(),
            tool_choice=tool_choice or "auto",
        )

    # ===================== System Prompt =====================
    def _get_system_prompt_content(self) -> str:
        """vision agent 的 system prompt：识图 + 媒体管理。"""
        return """
你是 vision agent，是 conversation 子系统按需调用的视觉处理 agent。
你需要根据用户的要求，识别图片内容并调用对应的工具进行操作。
并根据用户的要求，填写description。
若无特殊需求，自行判断图片内容为表情包或普通图片。
若判断为普通图片，调用 save_image 工具。
若判断为表情包，调用 save_sticker 工具。
description中必须包含对图片内容的详细描述，文字需要复述原文。
禁止不调用任何工具而直接输出。
"""

    @property
    def SYSTEM_PROMPT(self) -> str:
        return self._get_system_prompt_content()

    def get_system_prompt(self) -> str:
        return self.SYSTEM_PROMPT

    # ===================== 工具映射 =====================
    def _initialize_tool_functions(self) -> Dict[str, Any]:
        """返回 LLM 通过 function-calling 可调的 4 个工具映射。

        表情包库的列举 / 检索不在此处 —— 走上层 ToolVision.vision_agent 的
        「向量比对」（向量检索），不暴露给本 agent。
        """
        return {
            "save_image": self.save_image,
            "save_sticker": self.save_sticker,
            "adjust_sticker": self.adjust_sticker,
            "delete_sticker": self.delete_sticker,
        }

    def get_available_tools(self) -> List[Dict[str, Any]]:
        """暴露给 LLM 的工具 schema。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": "save_image",
                    "description": "保存普通图片。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title":         {"type": "string", "description": "图片标题。"},
                            "description":   {"type": "string", "description": "图片描述。"},
                            "mood":          {"type": "string", "description": "图片情绪。"},
                        },
                        "required": ["title", "description", "mood"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "save_sticker",
                    "description": "保存表情包",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "sticker_title":  {"type": "string", "description": "表情包标题。"},
                            "description":    {"type": "string", "description": "表情包描述。"},
                            "mood":           {"type": "string", "description": "表情包情绪。"},
                        },
                        "required": ["sticker_title", "description", "mood"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "adjust_sticker",
                    "description": (
                        "修改最近一次保存/调整过的表情包及其同名 .json 元数据。"
                        "会更新 sticker_title / description / mood；"
                        "若新标题与旧标题不同，还会同步重命名图片文件和元数据文件。"
                        "无需传入文件名，目标锁定在 agent 内部上下文。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "sticker_title":  {"type": "string", "description": "新的表情包标题。"},
                            "description":    {"type": "string", "description": "新的表情包描述。"},
                            "mood":           {"type": "string", "description": "新的表情包情绪。"},
                        },
                        "required": ["sticker_title", "description", "mood"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "delete_sticker",
                    "description": (
                        "删除最近一次保存/调整过的表情包及其同名 .json 元数据。"
                        "无需传入文件名，目标锁定在 agent 内部上下文。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {},
                    },
                },
            },
        ]

    # =====================================================================
    # 源图路径解析（供前处理与工具复用）
    # =====================================================================
    def _resolve_input_path(self, image_path: str) -> Path:
        """解析识图入口的图片路径（前处理专用）。

        接受四种形态：
        a) 绝对/相对路径直接存在 → 直接用
        b) 裸文件名：优先 LLM 自存目录，其次 MEDIA_ROOT 直接挂载，再次 MEDIA_ROOT 递归
        c) LLM 传错扩展名 → 用 .png/.jpg 替换再扫一轮
        d) 全部找不到 → 返回"未找到"友好提示路径（由调用方决定）
        """
        path = Path(image_path)
        if path.is_absolute() or path.exists():
            return path

        target_name = Path(image_path).name
        original_ext = Path(image_path).suffix.lower()

        # b1) LLM 侧平铺目录优先命中（自存优先）
        llm_hits: List[Path] = []
        for sub in (LLM_SAVE_STICKER_SUBDIR, LLM_SAVE_IMAGE_SUBDIR):
            sub_dir = LLM_SAVE_ROOT / sub
            if not sub_dir.is_dir():
                continue
            cand = sub_dir / target_name
            if cand.is_file():
                llm_hits.append(cand)
        if llm_hits:
            llm_hits.sort(key=lambda p: 0 if p.parent.name == LLM_SAVE_STICKER_SUBDIR else 1)
            llm_path = llm_hits[0]
            if llm_path.parent.name == LLM_SAVE_STICKER_SUBDIR:
                self._last_saved_sticker_filename = llm_path.name
            return llm_path

        # b2) MEDIA_ROOT 直接挂载（兼容旧的扁平布局）
        flat_candidate = MEDIA_ROOT / image_path
        if flat_candidate.exists():
            return flat_candidate

        # b3) MEDIA_ROOT 下递归查找同名文件
        hits = [p for p in MEDIA_ROOT.rglob(target_name) if p.is_file()]
        if hits:
            return max(hits, key=lambda p: p.stat().st_mtime)

        # c) 扩展名兜底
        suggested = self._suggest_alike_basename(target_name, original_ext)
        if suggested is not None:
            if suggested.parent.name == LLM_SAVE_STICKER_SUBDIR:
                self._last_saved_sticker_filename = suggested.name
            return suggested

        # d) 全部找不到 → 抛出友好提示（按遇错直接崩溃约束）
        raise FileNotFoundError(self._format_not_found_message(image_path, original_ext))

    # =====================================================================
    # 源图解析（供 save_image / save_sticker 复用）
    # =====================================================================
    def _resolve_source_image(self, image_filename: Optional[str]) -> Path:
        """解析源图路径。优先使用工具调用方传入的 image_filename（仅文件名）；
        否则回退到前处理注入的 self._current_source。失败直接崩溃阻断。
        """
        from brain.attachments import resolve_media_file

        if image_filename:
            return Path(resolve_media_file(image_filename))

        current = getattr(self, "_current_source", None)
        if current is None:
            raise RuntimeError(
                "save_image / save_sticker 未传入 image_filename，且当前 agent "
                "没有 run() 入口上下文（self._current_source=None）。"
            )
        return current

    # =====================================================================
    # 表情包目标解析（供 adjust_sticker / delete_sticker 复用）
    # =====================================================================
    def _resolve_current_sticker(self) -> Path:
        """解析"最近一次保存/调整过的表情包"文件路径，锁定目标。失败直接崩溃阻断。"""
        filename = getattr(self, "_last_saved_sticker_filename", None)
        if not filename:
            raise RuntimeError(
                "adjust_sticker / delete_sticker 找不到目标表情包："
                "当前 agent 没有 _last_saved_sticker_filename 上下文，"
                "必须先调用一次 save_sticker / adjust_sticker 才能再删除或调整。"
            )
        return self._resolve_sticker_file(filename)

    # =====================================================================
    # 辅助方法（static）
    # =====================================================================
    @staticmethod
    def _image_to_data_url(image_path: str, max_side: int = 1024, quality: int = 80) -> str:
        """读取本地图片，转成 base64 data URL。为省 token 自动缩放并 JPEG 重编码。"""
        from PIL import Image
        import io

        path = Path(image_path)
        if not path.exists():
            raise FileNotFoundError(f"图片不存在: {image_path}")

        img = Image.open(path)
        img.thumbnail((max_side, max_side))
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

        return f"data:image/jpeg;base64,{b64}"

    @staticmethod
    def _sanitize_filename(title: str, max_len: int = 50) -> str:
        """把标题清洗成可作为文件名的字符串。

        委托 brain.attachments.resolver.normalize_filename_component：全角标点折半角、
        白名单外字符（空格 / Windows 非法字符 / glob 通配符）一律降级为下划线。
        与发送侧 FILENAME_PATTERN 同源，保证「存得下」必然「发得出」。
        """
        from brain.attachments import normalize_filename_component
        return normalize_filename_component(title, max_len)

    @staticmethod
    def _resolve_sticker_file(sticker_filename: str) -> Path:
        """把 sticker_filename 解析成 llm_save/stickers/ 下的真实路径（防跨目录攻击）。"""
        from brain.attachments import resolve_media_file

        real_path = resolve_media_file(sticker_filename)
        p = Path(real_path).resolve()

        try:
            rel = p.relative_to(LLM_SAVE_ROOT)
            if rel.parts and rel.parts[0] == LLM_SAVE_STICKER_SUBDIR:
                return p
        except ValueError:
            pass

        try:
            rel = p.relative_to(MEDIA_ROOT)
            if rel.parts and rel.parts[0] == STICKER_SUBDIR:
                return p
        except ValueError:
            pass

        raise PermissionError(
            f"not a sticker file: {p} "
            f"(must be under {LLM_SAVE_ROOT / LLM_SAVE_STICKER_SUBDIR} "
            f"or {MEDIA_ROOT / STICKER_SUBDIR})"
        )

    # =====================================================================
    # 扩展名兜底 + 找不到时的友好提示
    # =====================================================================
    _ALT_EXTS: tuple = (".png", ".jpg")

    def _scan_basename(self, basename: str) -> List[Path]:
        """在 LLM 自存目录和 MEDIA_ROOT 下，按 basename 找文件，返回所有命中去重排序。"""
        seen: set = set()
        hits: List[Path] = []
        for sub in (LLM_SAVE_STICKER_SUBDIR, LLM_SAVE_IMAGE_SUBDIR):
            sub_dir = LLM_SAVE_ROOT / sub
            if not sub_dir.is_dir():
                continue
            cand = sub_dir / basename
            if cand.is_file() and str(cand) not in seen:
                hits.append(cand)
                seen.add(str(cand))
        flat = MEDIA_ROOT / basename
        if flat.is_file() and str(flat) not in seen:
            hits.append(flat)
            seen.add(str(flat))
        for p in MEDIA_ROOT.rglob(basename):
            if p.is_file() and str(p) not in seen:
                hits.append(p)
                seen.add(str(p))
        hits.sort(key=lambda p: (
            0 if p.parent.name == LLM_SAVE_STICKER_SUBDIR else 1,
            -p.stat().st_mtime,
        ))
        return hits

    def _suggest_alike_basename(self, target_name: str, original_ext: str) -> Optional[Path]:
        """当 target_name 找不到时，尝试替换扩展名再扫一轮，返回第一个命中。"""
        if not original_ext:
            return None
        stem = Path(target_name).stem
        if not stem:
            return None
        candidates = [ext for ext in self._ALT_EXTS if ext != original_ext]
        for ext in candidates:
            alt_name = f"{stem}{ext}"
            hits = self._scan_basename(alt_name)
            if hits:
                return hits[0]
        return None

    def _format_not_found_message(self, image_path: str, original_ext: str) -> str:
        """生成"未找到 xxx.<ext>"的友好提示。"""
        target_name = Path(image_path).name
        if not original_ext:
            return (
                f"未找到 {target_name}，请带上扩展名（如 .png / .jpg）后再试一次。"
            )

        stem = Path(target_name).stem
        candidates: List[str] = []
        seen: set = set()
        for ext in [e for e in self._ALT_EXTS if e != original_ext]:
            alt_name = f"{stem}{ext}"
            for p in self._scan_basename(alt_name):
                if p.name not in seen:
                    candidates.append(p.name)
                    seen.add(p.name)

        similar = [c for c in candidates if Path(c).stem == stem][:3]
        if similar:
            joined = " / ".join(similar)
            return f"未找到 {target_name}，是不是 {joined}？"
        return f"未找到 {target_name}"

    # =====================================================================
    # 表情包 / 图片管理工具（LLM 可调用）
    # =====================================================================
    async def save_image(
        self,
        title: str,
        description: str,
        mood: str,
        image_filename: Optional[str] = None,
    ) -> Dict[str, Any]:
        """工具：把一张图片保存到 memory/llm_save/images/，写同名 .json 元数据。"""
        missing = [name for name, val in (
            ("title", title),
            ("description", description),
            ("mood", mood),
        ) if not (isinstance(val, str) and val.strip())]
        if missing:
            return {
                "status": "error",
                "summary": f"保存图片失败：必填字段缺失 {', '.join(missing)}",
                "target": "",
            }

        src = self._resolve_source_image(image_filename)

        title = title.strip()
        description = description.strip()
        mood = mood.strip()

        safe_title = self._sanitize_filename(title)
        out_dir = LLM_SAVE_ROOT / LLM_SAVE_IMAGE_SUBDIR
        out_dir.mkdir(parents=True, exist_ok=True)

        suffix = src.suffix.lower() or ".jpg"
        out_image = out_dir / f"{safe_title}{suffix}"
        shutil.copy2(src, out_image)
        logger.info(f"[VisionAgent] 已保存图片: {src} → {out_image}")

        out_meta = out_image.with_suffix(".json")
        meta = {
            "title": title,
            "description": description,
            "mood": mood,
            "is_sticker": False,
            "original_path": str(src),
            "saved_image": str(out_image),
            "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(out_meta, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        saved_filename = out_image.name
        return {
            "status": "completed",
            "summary": f"已保存图片《{title}》[图片:{saved_filename}]，感觉：{mood}",
            "target": saved_filename,
            "title": title,
            "description": description,
            "mood": mood,
        }

    async def save_sticker(
        self,
        sticker_title: str,
        description: str,
        mood: str,
        image_filename: Optional[str] = None,
    ) -> Dict[str, Any]:
        """工具：把一张图片保存为表情包，复制到 memory/llm_save/stickers/（平铺）。"""
        missing = [name for name, val in (
            ("sticker_title", sticker_title),
            ("description", description),
            ("mood", mood),
        ) if not (isinstance(val, str) and val.strip())]
        if missing:
            return {
                "status": "error",
                "summary": f"保存表情包失败：必填字段缺失 {', '.join(missing)}",
                "target": "",
            }

        src = self._resolve_source_image(image_filename)

        sticker_title = sticker_title.strip()
        description = description.strip()
        mood = mood.strip()

        safe_title = self._sanitize_filename(sticker_title)
        out_dir = LLM_SAVE_ROOT / LLM_SAVE_STICKER_SUBDIR
        out_dir.mkdir(parents=True, exist_ok=True)

        suffix = src.suffix.lower() or ".jpg"
        out_image = out_dir / f"{safe_title}{suffix}"
        shutil.copy2(src, out_image)
        logger.info(f"[VisionAgent] 已保存表情包: {src} → {out_image}")

        out_meta = out_image.with_suffix(".json")
        meta = {
            "sticker_title": sticker_title,
            "description": description,
            "mood": mood,
            "is_sticker": True,
            "original_path": str(src),
            "saved_image": str(out_image),
            "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(out_meta, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        saved_filename = out_image.name
        # 维护最近表情包上下文：供后续 adjust_sticker / delete_sticker 锁定目标。
        self._last_saved_sticker_filename = saved_filename
        return {
            "status": "completed",
            "summary": f"已保存表情包《{sticker_title}》[表情:{saved_filename}]，感觉：{mood}",
            "target": saved_filename,
            "sticker_title": sticker_title,
            "description": description,
            "mood": mood,
        }

    async def adjust_sticker(
        self,
        sticker_title: str,
        description: str,
        mood: str,
    ) -> Dict[str, Any]:
        """工具：修改最近一次保存/调整过的表情包及其同名 .json。"""
        missing = [name for name, val in (
            ("sticker_title", sticker_title),
            ("description", description),
            ("mood", mood),
        ) if not (isinstance(val, str) and val.strip())]
        if missing:
            return {
                "status": "error",
                "summary": f"调整表情包失败：必填字段缺失 {', '.join(missing)}",
                "target": "",
            }

        sticker_title = sticker_title.strip()
        description = description.strip()
        mood = mood.strip()

        target = self._resolve_current_sticker()
        meta = target.with_suffix(".json")
        if not meta.exists():
            raise FileNotFoundError(
                f"adjust_sticker 找不到同名元数据: {meta}（表情包或已被外部改动）"
            )

        try:
            old_meta = json.loads(meta.read_text(encoding="utf-8"))
        except Exception:
            old_meta = {}
        old_title = (old_meta.get("sticker_title") or "").strip()
        new_safe_title = self._sanitize_filename(sticker_title)

        new_target = target
        new_meta_path = meta
        if old_title != sticker_title:
            suffix = target.suffix.lower() or ".jpg"
            new_target = LLM_SAVE_ROOT / LLM_SAVE_STICKER_SUBDIR / f"{new_safe_title}{suffix}"
            new_meta_path = new_target.with_suffix(".json")
            if new_target.exists() and new_target != target:
                raise FileExistsError(
                    f"adjust_sticker 重命名冲突：目标文件已存在 {new_target}，"
                    "请换一个新标题。"
                )
            target.rename(new_target)
            meta.rename(new_meta_path)

        new_meta = {
            "sticker_title": sticker_title,
            "description": description,
            "mood": mood,
            "is_sticker": True,
            "original_path": old_meta.get("original_path", ""),
            "saved_image": str(new_target),
            "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(new_meta_path, "w", encoding="utf-8") as f:
            json.dump(new_meta, f, ensure_ascii=False, indent=2)
        logger.info(
            f"[VisionAgent] 已调整表情包: {target} → {new_target}（标题: {old_title} → {sticker_title}）"
        )

        self._last_saved_sticker_filename = new_target.name

        renamed = new_target.name != target.name
        verb = "已重命名并调整" if renamed else "已调整"
        return {
            "status": "completed",
            "summary": (
                f"{verb}表情包为《{sticker_title}》[表情:{new_target.name}]，感觉：{mood}"
                + (f"（原名：{target.name}）" if renamed else "")
            ),
            "target": new_target.name,
            "sticker_title": sticker_title,
            "description": description,
            "mood": mood,
        }

    async def delete_sticker(self) -> Dict[str, Any]:
        """工具：删除最近一次保存/调整过的表情包及其同名 .json。"""
        target = self._resolve_current_sticker()
        meta = target.with_suffix(".json")
        deleted_filename = target.name

        target.unlink()
        logger.info(f"[VisionAgent] 已删除表情包: {target}")

        if meta.exists():
            meta.unlink()

        self._last_saved_sticker_filename = None

        return {
            "status": "completed",
            "summary": f"已删除表情包 [表情:{deleted_filename}]",
            "target": deleted_filename,
        }


