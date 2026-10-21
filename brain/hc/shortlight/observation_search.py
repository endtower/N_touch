"""锦囊查询关联见闻：正文向量匹配，全部排序返回，不截断、不修改原文。"""
import asyncio
import hashlib
import json
import math
from typing import List, Optional, Tuple

from brain.hc.database.core import get_memory_db, cosine_similarity, deserialize_vector, serialize_vector
from brain.hc.database.vector_store import get_vector_store
from config.settings import vector_config
from .notepad import get_shortlight_notepad, SLOT_EVENT


_search_lock = asyncio.Lock()


async def search_observations(query: str):
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query 必须描述当前遇到的困境")
    notes = await get_shortlight_notepad().get_slot_notes(SLOT_EVENT)
    notes = [note for note in notes if (note.get("text") or "").strip()]
    base = {"query": query.strip(), "total": len(notes), "count": 0, "observations": []}
    if not notes:
        return {**base, "status": "empty", "hint": "见闻录暂无记录。"}
    vector_store = get_vector_store()
    query_vector = await vector_store.generate_vector(query.strip(), target="bequest")
    if not query_vector:
        return {**base, "status": "disabled", "hint": "向量检索已停用，未进行见闻匹配；可用 list_notes 查看见闻录。"}
    model_key = hashlib.sha256(json.dumps([
        vector_config.BASE_URL, vector_config.MODEL_NAME, len(query_vector),
    ]).encode()).hexdigest()
    matches = []
    async with _search_lock:
        conn = await get_memory_db()._ensure_connection()
        cursor = await conn.execute("SELECT * FROM observation_vectors WHERE model_key=?", (model_key,))
        cached = {row["note_id"]: dict(row) for row in await cursor.fetchall()}
        # 先挑出缓存缺失/过期的见闻，再一次性批量补算（内部按 API 上限 10 条分块）
        note_vectors: List[Optional[List[float]]] = []
        pending: List[Tuple[int, str, str, str]] = []  # (notes 下标, 正文, note_id, 正文指纹)
        for note in notes:
            text = note["text"].strip()
            content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            note_id = note.get("note_id") or content_hash
            old = cached.get(note_id)
            vector = deserialize_vector(old["vector"]) if old and old["content_hash"] == content_hash else None
            if vector and len(vector) == len(query_vector):
                note_vectors.append(vector)
            else:
                note_vectors.append(None)
                pending.append((len(note_vectors) - 1, text, note_id, content_hash))
        if pending:
            fresh = await vector_store.generate_vectors([item[1] for item in pending], target="bequest")
            if not fresh:
                return {**base, "status": "disabled", "hint": "向量检索被停用，未完成见闻匹配。"}
            if len(fresh) != len(pending):
                raise ValueError("见闻批量向量结果条数与待补算条数不一致，请检查嵌入服务")
            for (index, _text, note_id, content_hash), vector in zip(pending, fresh):
                if len(vector) != len(query_vector):
                    raise ValueError("见闻向量与查询向量维度不一致，请检查嵌入服务")
                note_vectors[index] = vector
                await conn.execute(
                    "INSERT OR REPLACE INTO observation_vectors VALUES (?, ?, ?, ?)",
                    (note_id, content_hash, model_key, serialize_vector(vector)),
                )
            await conn.commit()
        for note, vector in zip(notes, note_vectors):
            if vector is None:
                raise RuntimeError("见闻向量补算后仍缺失，状态异常")
            score = cosine_similarity(query_vector, vector)
            if not math.isfinite(score):
                raise ValueError("见闻向量相似度无效，请检查嵌入服务")
            matches.append({**note, "similarity": round(score, 4)})
    matches.sort(key=lambda note: note["similarity"], reverse=True)
    return {**base, "status": "matched", "count": len(matches), "observations": matches,
            "hint": "全部见闻按向量相似度排序；相似度代表相关程度，不代表事实可信度。"
                    + ("现有见闻不足5条，已返回全部真实记录。" if len(matches) < 5 else "")}
