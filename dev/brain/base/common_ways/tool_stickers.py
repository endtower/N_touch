# -*- coding: utf-8 -*-
"""供会话和 Web 共用的表情包库工具。"""

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from brain.attachments import normalize_filename_component, resolve_media_file
from brain.attachments.preflight import ALLOWED_EXTS
from brain.attachments.sticker_store import STICKER_DIR, archive_sticker
from utils.logger import get_logger

logger = get_logger('ToolStickers', "INFO")

# 向量比对返回条数（不暴露给 LLM，避免工具参数膨胀）
VECTOR_TOP_K = 5

# 向量索引文件名：与表情包图片 / meta 平铺在同一目录，记录 {文件名: {hash, dim, vector, vectorized_at}}。
# `_iter_stickers` 只认图片后缀，不会把它当表情包读进来。
VECTOR_INDEX_FILENAME = "_vectors.json"


class ToolStickers:
    """表情包检索和管理，由会话模型调用。"""

    @staticmethod
    def get_search_stickers_tool_schema() -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "search_stickers",
                "description": (
                    "按准确标题、文件名或期望用途查找自己的表情包，返回简介和文件名。"
                    "要发送、修改或删除某个表情包，把结果 [表情:文件名] 中的文件名传给对应工具。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "表情包标题、文件名或期望用途，例如‘用来表达无语’",
                        },
                    },
                    "required": ["query"],
                },
            },
        }

    @staticmethod
    def get_manage_sticker_tool_schema() -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "manage_sticker",
                "description": (
                    "保存、修改或删除自己库里的表情包。保存时提供图片文件名和简介。"
                    "修改或删除时使用 search_stickers 或先前保存结果里的准确文件名。"
                    "等工具确认后再告诉对方操作完成。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["保存", "修改", "删除"]},
                        "target": {
                            "type": "string",
                            "description": "只填文件名。保存用聊天中的图片文件名，修改或删除用库内文件名",
                        },
                        "title": {
                            "type": "string",
                            "description": "简短标题。保存时留空则从简介取标题；修改标题会同步更新文件名",
                        },
                        "description": {
                            "type": "string",
                            "description": "简述画面和用途。保存时必填，修改时可选",
                        },
                        "mood": {
                            "type": "string",
                            "description": "可选的情绪或语气，如‘撒娇’或‘无语’",
                        },
                    },
                    "required": ["action", "target"],
                },
            },
        }

    @classmethod
    async def manage_sticker(
        cls,
        action: str,
        target: str,
        title: str = "",
        description: str = "",
        mood: str = "",
    ) -> str:
        if not all(isinstance(value, str) for value in (action, target, title, description, mood)):
            raise TypeError("表情包管理工具的参数必须是字符串。")
        action = action.strip()
        target = target.strip()
        title = title.strip()
        description = description.strip()
        mood = mood.strip()

        if not target:
            return "操作失败：请提供目标图片的文件名。"
        if action == "保存":
            if not description:
                return "保存表情包失败：请提供 description（表情包简介）。"
            return await cls._save_sticker(
                target, title=title or description[:24], summary=description, mood=mood
            )
        if action == "修改":
            if not (title or description or mood):
                return "修改表情包失败：请至少提供标题、简介或情绪中的一项。"
            return await cls._adjust_sticker(target, new_title=title, summary=description, mood=mood)
        if action == "删除":
            return await cls._delete_sticker(target)
        return f"未知的表情包操作：{action}（支持保存、修改、删除）。"

    # ============ 向量索引（一键向量化的落盘载体） ============

    @classmethod
    def _sticker_root(cls) -> Path:
        """表情包库根目录（memory/llm_save/stickers/）。"""
        return STICKER_DIR

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
                logger.warning(f"[ToolStickers] 跳过无文字描述的表情包（无可向量化文本）: {filename}")
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
        logger.info(f"[ToolStickers] 一键向量化完成: {result}")
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
    async def search_stickers(cls, query: str) -> str:
        """先匹配准确标题和文件名，再按语义向量检索表情包库。

        优先消费「一键向量化」落盘的缓存（_vectors.json，文本指纹一致即直接复用），
        缺失 / 过期的条目现场补算且不落盘——缓存只提速降本，检索语义与过去一致。
        """
        if not isinstance(query, str):
            raise TypeError("query 必须是字符串。")
        query = query.strip()
        if not query:
            return "搜索表情包失败：请提供标题、文件名或用途。"

        stickers = [meta for _, _, meta in cls._iter_stickers()]
        if not stickers:
            return "表情包库为空。"

        exact_query = query.casefold()
        exact = [
            sticker for sticker in stickers
            if exact_query in (
                sticker["filename"].casefold(),
                (sticker.get("sticker_title") or sticker.get("title") or "").strip().casefold(),
            )
        ]
        if exact:
            return cls._format_search_results([(sticker, None) for sticker in exact])

        from brain.hc.database.core import cosine_similarity

        index = cls._load_vector_index()
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

        return cls._format_search_results(top)

    @staticmethod
    def _format_search_results(matches: List[Tuple[Dict[str, Any], Optional[float]]]) -> str:
        lines = [f"已找到 {len(matches)} 个表情包："]
        for idx, (sticker, similarity) in enumerate(matches, 1):
            filename = sticker["filename"]
            title = sticker.get("sticker_title") or sticker.get("title") or filename
            detail = " | ".join(text for text in (sticker.get("description"), sticker.get("mood")) if text)
            line = f"{idx}. [表情:{filename}] {title}"
            if similarity is not None:
                line += f"（相似度 {similarity:.3f}）"
            if detail:
                line += f" — {detail}"
            lines.append(line)
        return "\n".join(lines)

    # ============ 表情包库遍历 / 定位 ============

    @classmethod
    def _iter_stickers(cls) -> Iterator[Tuple[Path, Path, Dict[str, Any]]]:
        """遍历表情包库，产出 (图片路径, meta 路径, meta 内容)。

        meta 文件损坏时 json.JSONDecodeError 直接上抛崩溃阻断（不吞成空 dict——
        标题读空会让查找静默失配，把问题藏起来）。
        """
        sticker_root = cls._sticker_root()
        if not sticker_root.exists():
            return
        for img_path in sticker_root.iterdir():
            if not img_path.is_file():
                continue
            if img_path.suffix.lower() not in ALLOWED_EXTS:
                continue
            meta_path = img_path.with_suffix(".json")
            if not meta_path.exists():
                continue
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["filename"] = img_path.name
            yield img_path, meta_path, meta

    @classmethod
    def _find_sticker(cls, filename: str) -> Optional[Tuple[Path, Path, Dict[str, Any]]]:
        """仅按库内的准确文件名定位写入目标。"""
        return next((entry for entry in cls._iter_stickers() if entry[0].name == filename), None)

    # ============ 保存表情包 ============

    @classmethod
    async def _save_sticker(cls, image_filename: str, title: str, summary: str, mood: str) -> str:
        """按主模型提交的元数据直接归档图片。"""
        try:
            source = Path(resolve_media_file(image_filename))
            saved, created = archive_sticker(source, title, summary, mood)
        except FileNotFoundError:
            return f"保存表情包失败：本地找不到图片 {image_filename}。请核对文件名或请主人重发。"
        except PermissionError:
            return f"保存表情包失败：无法访问图片 {image_filename} 或写入表情包库。"
        except ValueError as exc:
            return f"保存表情包失败：{exc}"
        if not created:
            return f"这张图已在表情包库中 [表情:{saved.name}]。"
        logger.info(f"[ToolStickers] 已保存表情包: {image_filename} → {saved.name}")
        return f"已保存表情包《{title}》[表情:{saved.name}]。"

    # ============ 删除表情包 ============

    @classmethod
    async def _delete_sticker(cls, target: str) -> str:
        found = cls._find_sticker(target)
        if found is None:
            return f"未找到文件名为「{target}」的表情包。请先搜索表情包库并使用返回的文件名。"
        img_path, meta_path, _ = found
        img_path.unlink()
        if meta_path.exists():
            meta_path.unlink()
        logger.info(f"[ToolStickers] 已删除表情包: {img_path.name}（target：{target}）")
        return f"已删除表情包「{img_path.name}」。"

    # ============ 修改表情包 ============

    @classmethod
    async def _adjust_sticker(cls, target: str, new_title: str = "", summary: str = "", mood: str = "") -> str:
        found = cls._find_sticker(target)
        if found is None:
            return f"未找到文件名为「{target}」的表情包。请先搜索表情包库并使用返回的文件名。"
        img_path, meta_path, meta = found

        old_title = (meta.get("sticker_title") or meta.get("title") or "").strip()
        final_title = new_title or old_title or target
        final_description = summary or meta.get("description", "")

        # 标题变更：同步重命名图片与 meta 文件。
        renamed = False
        new_img = img_path
        new_meta = meta_path
        if final_title != old_title:
            safe_title = normalize_filename_component(final_title)
            new_img = img_path.parent / f"{safe_title}{img_path.suffix.lower() or '.jpg'}"
            new_meta = new_img.with_suffix(".json")
            if new_img.exists() and new_img != img_path:
                return f"修改表情包失败：新标题对应的文件已存在 {new_img.name}，请换一个标题。"
            if new_meta.exists() and new_meta != meta_path:
                return f"修改表情包失败：表情包库已有同名条目，请换一个标题。"
            if new_img != img_path:
                img_path.rename(new_img)
                meta_path.rename(new_meta)
                renamed = True

        meta["sticker_title"] = final_title
        meta["description"] = final_description
        if mood:
            meta["mood"] = mood
        meta["saved_image"] = str(new_img)
        meta["saved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        meta.pop("filename", None)  # 遍历时注入的辅助字段，不落库
        with open(new_meta, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        logger.info(
            f"[ToolStickers] 已修改表情包: {img_path.name} → {new_img.name}（标题: {old_title} → {final_title}）"
        )
        verb = "已重命名并修改" if renamed else "已修改"
        return (
            f"{verb}表情包为《{final_title}》[表情:{new_img.name}]，简介：{final_description}"
            + (f"（原名：{img_path.name}）" if renamed else "")
        )
