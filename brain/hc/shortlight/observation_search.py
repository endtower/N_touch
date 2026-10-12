"""锦囊查询关联见闻：正文向量匹配，全部排序返回，不截断、不修改原文。"""
import asyncio
import hashlib
import json
import math

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
        for note in notes:
            text = note["text"].strip()
            content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            note_id = note.get("note_id") or content_hash
            old = cached.get(note_id)
            vector = deserialize_vector(old["vector"]) if old and old["content_hash"] == content_hash else None
            if not vector or len(vector) != len(query_vector):
                vector = await vector_store.generate_vector(text, target="bequest")
                if not vector:
                    return {**base, "status": "disabled", "hint": "向量检索被停用，未完成见闻匹配。"}
                if len(vector) != len(query_vector):
                    raise ValueError("见闻向量与查询向量维度不一致，请检查嵌入服务")
                await conn.execute(
                    "INSERT OR REPLACE INTO observation_vectors VALUES (?, ?, ?, ?)",
                    (note_id, content_hash, model_key, serialize_vector(vector)),
                )
                await conn.commit()
            score = cosine_similarity(query_vector, vector)
            if not math.isfinite(score):
                raise ValueError("见闻向量相似度无效，请检查嵌入服务")
            matches.append({**note, "similarity": round(score, 4)})
    matches.sort(key=lambda note: note["similarity"], reverse=True)
    return {**base, "status": "matched", "count": len(matches), "observations": matches,
            "hint": "全部见闻按向量相似度排序；相似度代表相关程度，不代表事实可信度。"
                    + ("现有见闻不足5条，已返回全部真实记录。" if len(matches) < 5 else "")}
