# -*- coding: utf-8 -*-
"""通用工具：ToolVision（vision_agent / vision_agent_adjust）

识图 / 表情包管理工具的通用实现，继承 ToolHandlers。

依赖 VisionAgent（识图 agent），位于 brain/base/light_agent/vision_agent.py。

对外两个工具（2026-09-22 由单个 vision_agent 拆为两工具，参数收敛到最小集）：

- `vision_agent(type, context)`：查询类
  - 向量比对：context = 期望用途的表情包描述 → 语义检索表情包库
  - 识别图片：context = 图片标题 / 图片名称 → 只识图返回描述（不落库）
- `vision_agent_adjust(type, target, new_title, summary)`：管理类
  - 保存：把图片保存为表情包（target = 图片名称，summary = 简介）
  - 删除：按标题或文件名删除（target）
  - 修改：改标题 / 简介（target = 原标题或名称）

设计取舍（2026-09-21/22 两轮）：
① 最初把四个操作挤进单个 `vision_agent`，用 `context` JSON 字符串承载参数——
   context 是嵌在 function arguments 里的字符串，模型须输出转义后的 JSON，
   标题含引号顿号时易吐非法 JSON；且"必填"只能写在 description 散文里。
② 归因清楚后按「查询 / 管理」分两个工具，参数只保留真正必要的字段：
   查询类只有 type + context（context 对两个操作各是一个自然语言字符串，无结构化解析），
   管理类只有 type + target + 可选 new_title + summary。

注：识图原始能力（调用 VisionAgent）为内部方法 call_vision_agent，
供识别图片与保存复用，不作为独立工具暴露；conversation 专有的"识图记录"
（add_recognized_image）仍由调用方自行处理，不放入通用方法。
"""

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from utils.logger import get_logger
from brain.base.common_ways.tool_handlers import ToolHandlers

logger = get_logger('ToolVision', "INFO")

# 向量比对返回条数（不暴露给 LLM，避免工具参数膨胀）
VECTOR_TOP_K = 5

# 向量索引文件名：与表情包图片 / meta 平铺在同一目录，记录 {文件名: {hash, dim, vector, vectorized_at}}。
# `_iter_stickers` 只认图片后缀，不会把它当表情包读进来。
VECTOR_INDEX_FILENAME = "_vectors.json"


class ToolVision(ToolHandlers):
    """vision 工具：vision_agent（向量比对 / 识别图片）+ vision_agent_adjust（保存 / 删除 / 修改）。"""

    # ============ 工具 schema（提示词） ============

    @classmethod
    def get_vision_agent_tool_schema(cls) -> Dict[str, Any]:
        """vision_agent：查询类（向量比对 / 识别图片），只有 type + context 两个参数。"""
        return {
            "type": "function",
            "function": {
                "name": "vision_agent",
                "description": (
                    "视觉查询工具。type 指定功能类型：\n"
                    "- 向量比对：按语义检索表情包库，返回最相似的表情包。"
                    "context 传「期望用途的表情包描述」（如「用来表达无语」）。\n"
                    "- 识别图片：识别一张图片的内容并返回描述。"
                    "context 传「图片标题 / 图片名称」（与消息中 [图片:文件名] 一致）。\n"
                    "保存 / 删除 / 修改表情包请改用 vision_agent_adjust。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["向量比对", "识别图片"],
                            "description": "功能类型：向量比对=按语义检索表情包库 / 识别图片=识别图片内容",
                        },
                        "context": {
                            "type": "string",
                            "description": (
                                "向量比对：期望用途的表情包描述；"
                                "识别图片：图片标题 / 图片名称（与消息 [图片:文件名] 一致）"
                            ),
                        },
                    },
                    "required": ["type", "context"],
                },
            },
        }

    @classmethod
    def get_vision_agent_adjust_tool_schema(cls) -> Dict[str, Any]:
        """vision_agent_adjust：管理类（保存 / 删除 / 修改）。

        required 只放 type + target：summary 对「删除」无意义，
        其"保存 / 修改必填"由 description 说明 + 入口方法运行时校验（缺则返回失败文案）。
        """
        return {
            "type": "function",
            "function": {
                "name": "vision_agent_adjust",
                "description": (
                    "表情包库管理工具。type 指定操作：\n"
                    "- 保存：把一张图片保存为表情包（需给出简介）。\n"
                    "- 删除：按标题或文件名删除表情包（无需简介）。\n"
                    "- 修改：修改表情包的标题 / 简介（改标题会同步重命名文件）。\n"
                    "target 传图片原标题或图片名称。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["保存", "删除", "修改"],
                            "description": "操作类型：保存 / 删除 / 修改",
                        },
                        "target": {
                            "type": "string",
                            "description": (
                                "图片原标题或图片名称。"
                                "保存在此传待保存图片的文件名（与消息 [图片:文件名] 一致）；"
                                "删除 / 修改传目标表情包的原标题或文件名"
                            ),
                        },
                        "new_title": {
                            "type": "string",
                            "description": "表情包新标题（可选）：修改时提供才会改标题，保存时可直接指定标题",
                        },
                        "summary": {
                            "type": "string",
                            "description": "表情包简介，用于识别与标记表情包库：保存与修改必填，删除可留空",
                        },
                    },
                    "required": ["type", "target"],
                },
            },
        }

    # ============ vision_agent：查询类入口 ============

    @classmethod
    async def vision_agent(cls, type: str, context: str = "") -> str:
        """视觉查询入口：向量比对 / 识别图片。

        Args:
            type: 功能类型，取值「向量比对」/「识别图片」。
            context: 向量比对时传期望用途的表情包描述；识别图片时传图片标题 / 名称。

        Returns:
            操作结果文案（成功 / 失败说明）。
        """
        op = (type or "").strip()
        ctx = (context or "").strip()

        if op == "向量比对":
            if not ctx:
                return "向量比对失败：context 缺少期望用途的表情包描述。"
            return await cls._vector_search_stickers(ctx)

        if op == "识别图片":
            if not ctx:
                return "识别图片失败：context 缺少图片标题 / 图片名称。"
            return await cls._recognize_image(ctx)

        return f"未知的 vision_agent 功能类型：{op}（仅支持 向量比对 / 识别图片）"

    # ============ vision_agent_adjust：管理类入口 ============

    @classmethod
    async def vision_agent_adjust(
        cls,
        type: str,
        target: str,
        new_title: str = "",
        summary: str = "",
    ) -> str:
        """表情包管理入口：保存 / 删除 / 修改。

        Args:
            type: 操作类型，取值「保存」/「删除」/「修改」。
            target: 图片原标题或图片名称（保存时为待保存图片的文件名）。
            new_title: 图片新标题（可选）。
            summary: 图片 / 表情包简介（保存与修改必填，删除可留空）。

        Returns:
            操作结果文案（成功 / 失败说明）。
        """
        op = (type or "").strip()
        target = (target or "").strip()
        new_title = (new_title or "").strip()
        summary = (summary or "").strip()

        if op == "保存":
            if not target:
                return "保存表情包失败：缺少 target（待保存图片的名称）。"
            if not summary:
                return "保存表情包失败：缺少 summary（表情包简介）。"
            return await cls._save_sticker(target, title=new_title, summary=summary)

        if op == "删除":
            if not target:
                return "删除表情包失败：缺少 target（原标题或图片名称）。"
            return await cls._delete_sticker(target)

        if op == "修改":
            if not target:
                return "修改表情包失败：缺少 target（原标题或图片名称）。"
            if not (new_title or summary):
                return "修改表情包失败：new_title / summary 至少提供一项。"
            return await cls._adjust_sticker(target, new_title=new_title, summary=summary)

        return f"未知的 vision_agent_adjust 操作类型：{op}（仅支持 保存 / 删除 / 修改）"

    # ============ 识别图片 ============

    @classmethod
    async def _recognize_image(cls, image_filename: str) -> str:
        """识别图片内容并返回描述（只识图，不落库）。"""
        result = await cls.call_vision_agent(image_filename)
        return result.get("content") or "vision agent 未返回任何信息。"

    # ============ 向量索引（一键向量化的落盘载体） ============

    @classmethod
    def _sticker_root(cls) -> Path:
        """表情包库根目录（memory/llm_save/stickers/）。"""
        from brain.base.light_agent.vision_agent import LLM_SAVE_ROOT, LLM_SAVE_STICKER_SUBDIR
        return LLM_SAVE_ROOT / LLM_SAVE_STICKER_SUBDIR

    @classmethod
    def _sticker_text(cls, meta: Dict[str, Any]) -> str:
        """表情包的向量化文本：标题 + 描述 + 情绪。

        向量化与检索两侧共用本方法，保证「存进去的」与「查出来的」是同一段文本。
        """
        return " ".join(
            x for x in (
                meta.get("sticker_title") or meta.get("title", ""),
                meta.get("description", ""),
                meta.get("mood", ""),
            ) if x
        ).strip()

    @staticmethod
    def _text_fingerprint(text: str) -> str:
        """向量化文本的指纹：文本有任何改动，指纹随之变化（即缓存过期）。"""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @classmethod
    def _load_vector_index(cls) -> Dict[str, Any]:
        """读取向量索引；文件不存在返回空表，内容损坏直接上抛崩溃阻断。"""
        path = cls._sticker_root() / VECTOR_INDEX_FILENAME
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    @classmethod
    def _save_vector_index(cls, index: Dict[str, Any]) -> None:
        """整体覆写向量索引（索引由现存文件全量重建，天然清理已删除 / 改名的条目）。"""
        path = cls._sticker_root() / VECTOR_INDEX_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False)

    @classmethod
    def is_vectorized(cls, meta: Dict[str, Any]) -> bool:
        """单条表情包是否已向量化且未过期（文本指纹与索引一致）。"""
        filename = meta.get("filename") or ""
        entry = cls._load_vector_index().get(filename) if filename else None
        if not entry or not entry.get("vector"):
            return False
        return entry.get("hash") == cls._text_fingerprint(cls._sticker_text(meta))

    @classmethod
    def vectorization_status(cls) -> Dict[str, bool]:
        """全库向量化状态 {文件名: 是否已向量化且未过期}（供前端展示待向量化数量）。"""
        index = cls._load_vector_index()
        status: Dict[str, bool] = {}
        for _, _, meta in cls._iter_stickers():
            entry = index.get(meta["filename"])
            status[meta["filename"]] = bool(
                entry
                and entry.get("vector")
                and entry.get("hash") == cls._text_fingerprint(cls._sticker_text(meta))
            )
        return status

    @classmethod
    async def vectorize_all_stickers(cls, force: bool = False) -> Dict[str, Any]:
        """一键向量化：为表情包库全部条目生成向量并写回索引文件。

        Args:
            force: True 时忽略已有缓存全量重算（更换嵌入模型 / 维度变更后用）；
                默认只补「缺失」与「文本已改动而过期」的条目。

        Returns:
            {"total", "embedded", "reused", "skipped", "vectorized_at"}。

        Raises:
            RuntimeError: 嵌入服务未返回向量（崩溃阻断，不静默写下空向量）。
        """
        index = cls._load_vector_index()
        new_index: Dict[str, Any] = {}
        pending: List[Tuple[str, str, str]] = []  # (文件名, 文本, 指纹)
        reused = 0
        skipped = 0

        for _, _, meta in cls._iter_stickers():
            filename = meta["filename"]
            text = cls._sticker_text(meta)
            if not text:
                skipped += 1
                logger.warning(f"[ToolVision] 跳过无文字描述的表情包（无可向量化文本）: {filename}")
                continue
            fingerprint = cls._text_fingerprint(text)
            cached = index.get(filename)
            if not force and cached and cached.get("vector") and cached.get("hash") == fingerprint:
                new_index[filename] = cached
                reused += 1
            else:
                pending.append((filename, text, fingerprint))

        if pending:
            vectors = await cls._embed_texts([t for _, t, _ in pending])
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for (filename, _text, fingerprint), vector in zip(pending, vectors):
                if not vector:
                    raise RuntimeError(f"表情包《{filename}》向量化失败：嵌入服务未返回向量")
                new_index[filename] = {
                    "hash": fingerprint,
                    "dim": len(vector),
                    "vector": vector,
                    "vectorized_at": now,
                }

        cls._save_vector_index(new_index)

        result = {
            "total": len(new_index),
            "embedded": len(pending),
            "reused": reused,
            "skipped": skipped,
            "vectorized_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        logger.info(f"[ToolVision] 一键向量化完成: {result}")
        return result

    # ============ 向量比对（表情包检索） ============

    @classmethod
    async def _embed_text(cls, text: str) -> Optional[List[float]]:
        """生成单条文本向量（直接走嵌入服务，不受 ONLY_BEQUEST_VECTORIZATION 目标过滤限制）。"""
        return (await cls._embed_texts([text]))[0]

    @classmethod
    async def _embed_texts(cls, texts: List[str]) -> List[List[float]]:
        """批量生成文本向量（内部按嵌入 API 上限自动分块）。"""
        from config.settings import vector_config
        from brain.hc.database.embedding_service import (
            init_embedding_service,
            get_embedding_service,
        )

        init_embedding_service(
            api_key=vector_config.API_KEY,
            api_base=vector_config.BASE_URL,
            model=vector_config.MODEL_NAME,
        )
        svc = get_embedding_service()
        return await svc.generate_embeddings(list(texts))

    @classmethod
    async def _vector_search_stickers(cls, query: str) -> str:
        """按语义向量检索表情包库（标题+描述+情绪 拼接文本做嵌入，余弦比对）。

        优先消费「一键向量化」落盘的缓存（_vectors.json，文本指纹一致即直接复用），
        缺失 / 过期的条目现场补算且不落盘——缓存只提速降本，检索语义与过去一致。
        """
        from brain.hc.database.core import cosine_similarity

        index = cls._load_vector_index()
        stickers = [meta for _, _, meta in cls._iter_stickers()]
        if not stickers:
            return "表情包库为空。你可以找主人要表情包。"

        texts = [cls._sticker_text(s) for s in stickers]

        vecs: List[Optional[List[float]]] = []
        stale: List[int] = []
        for i, (s, text) in enumerate(zip(stickers, texts)):
            entry = index.get(s["filename"])
            if entry and entry.get("vector") and entry.get("hash") == cls._text_fingerprint(text):
                vecs.append(entry["vector"])
            else:
                vecs.append(None)
                stale.append(i)

        query_vec = await cls._embed_text(query)
        if stale:
            fresh = await cls._embed_texts([texts[i] for i in stale])
            for i, vec in zip(stale, fresh):
                vecs[i] = vec

        scored: List[Tuple[Dict[str, Any], float]] = []
        for s, vec in zip(stickers, vecs):
            if query_vec and vec and len(vec) == len(query_vec):
                scored.append((s, cosine_similarity(query_vec, vec)))
        scored.sort(key=lambda x: x[1], reverse=True)

        top = scored[:VECTOR_TOP_K]
        if not top:
            return "未找到匹配的表情包。你可以找主人要表情包。"

        lines = [f"已找到 {len(top)} 个最相似的表情包："]
        for idx, (s, sim) in enumerate(top, 1):
            title = s.get("sticker_title") or s.get("title") or s.get("filename") or ""
            tail = " | ".join(x for x in (s.get("description"), s.get("mood")) if x)
            line = f"{idx}. [表情:{s['filename']}] {title}（相似度 {sim:.3f}）"
            if tail:
                line += f" — {tail}"
            lines.append(line)
        return "\n".join(lines) + "\n你可以找主人要表情包。"

    # ============ 表情包库遍历 / 定位 ============

    @classmethod
    def _iter_stickers(cls) -> Iterator[Tuple[Any, Any, Dict[str, Any]]]:
        """遍历表情包库，产出 (图片路径, meta 路径, meta 内容)。

        meta 文件损坏时 json.JSONDecodeError 直接上抛崩溃阻断（不吞成空 dict——
        标题读空会让查找静默失配，把问题藏起来）。
        """
        from brain.base.light_agent.vision_agent import LLM_SAVE_ROOT, LLM_SAVE_STICKER_SUBDIR

        sticker_root = LLM_SAVE_ROOT / LLM_SAVE_STICKER_SUBDIR
        if not sticker_root.exists():
            return
        for img_path in sticker_root.iterdir():
            if not img_path.is_file():
                continue
            if img_path.suffix.lower() not in (".jpg", ".jpeg", ".png", ".gif"):
                continue
            meta_path = img_path.with_suffix(".json")
            if not meta_path.exists():
                continue
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["filename"] = img_path.name
            yield img_path, meta_path, meta

    @classmethod
    def _find_sticker(cls, target: str) -> Optional[Tuple[Any, Any, Dict[str, Any]]]:
        """按标题或文件名定位表情包；未命中返回 None。"""
        target = (target or "").strip()
        if not target:
            return None
        for img_path, meta_path, meta in cls._iter_stickers():
            if img_path.name == target:
                return img_path, meta_path, meta
            title = (meta.get("sticker_title") or meta.get("title") or "").strip()
            if title == target:
                return img_path, meta_path, meta
        return None

    # ============ 保存表情包 ============

    @classmethod
    async def _save_sticker(cls, image_filename: str, title: str = "", summary: str = "") -> str:
        """保存表情包：交请 VisionAgent 识图并落库（标题 / 简介由调用方直接指定）。"""
        parts = ["请将这张图片登记为表情包（调用 save_sticker）"]
        if title:
            parts.append(f"标题：{title}")
        if summary:
            parts.append(f"简介：{summary}")

        result = await cls.call_vision_agent(image_filename, "；".join(parts))
        return result.get("content") or "vision agent 未返回任何信息。"

    # ============ 删除表情包 ============

    @classmethod
    async def _delete_sticker(cls, target: str) -> str:
        found = cls._find_sticker(target)
        if found is None:
            return f"未找到标题或名称为「{target}」的表情包。"
        img_path, meta_path, _ = found
        img_path.unlink()
        if meta_path.exists():
            meta_path.unlink()
        logger.info(f"[ToolVision] 已删除表情包: {img_path.name}（target：{target}）")
        return f"已删除表情包 [表情:{img_path.name}]"

    # ============ 修改表情包 ============

    @classmethod
    async def _adjust_sticker(cls, target: str, new_title: str = "", summary: str = "") -> str:
        found = cls._find_sticker(target)
        if found is None:
            return f"未找到标题或名称为「{target}」的表情包。"
        img_path, meta_path, meta = found

        old_title = (meta.get("sticker_title") or meta.get("title") or "").strip()
        final_title = new_title or old_title or target
        final_description = summary or meta.get("description", "")

        # 标题变更：同步重命名图片与 meta 文件（复用 VisionAgent 的文件名清洗规则）
        renamed = False
        new_img = img_path
        new_meta = meta_path
        if final_title != old_title:
            from brain.base.light_agent.vision_agent import VisionAgent
            safe_title = VisionAgent._sanitize_filename(final_title)
            new_img = img_path.parent / f"{safe_title}{img_path.suffix.lower() or '.jpg'}"
            new_meta = new_img.with_suffix(".json")
            if new_img.exists() and new_img != img_path:
                return f"修改表情包失败：新标题对应的文件已存在 {new_img.name}，请换一个标题。"
            img_path.rename(new_img)
            if meta_path.exists() and new_meta != meta_path:
                meta_path.rename(new_meta)
            renamed = True

        meta["sticker_title"] = final_title
        meta["description"] = final_description
        meta["saved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        meta.pop("filename", None)  # 遍历时注入的辅助字段，不落库
        with open(new_meta, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        logger.info(
            f"[ToolVision] 已修改表情包: {img_path.name} → {new_img.name}（标题: {old_title} → {final_title}）"
        )
        verb = "已重命名并修改" if renamed else "已修改"
        return (
            f"{verb}表情包为《{final_title}》[表情:{new_img.name}]，简介：{final_description}"
            + (f"（原名：{img_path.name}）" if renamed else "")
        )

    # ============ 识图原始能力（内部方法，不作为独立工具暴露） ============

    @classmethod
    async def call_vision_agent(cls, image_filename: str, context_hint: str = None) -> Dict[str, Any]:
        """识图：调用 VisionAgent 识别图片并返回结果。

        只负责识图并返回结果 dict；conversation 专有的"识图记录"由调用方自行处理。
        供「识别图片」与「保存」复用，不再作为独立工具 schema 暴露。

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