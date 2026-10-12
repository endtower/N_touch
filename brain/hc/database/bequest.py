# -*- coding: utf-8 -*-
"""
嘱托/经验操作类（SQLite）

经验是独立表，支持两类经验（category 字段，2026-09-24 起不再固定话术专用）：
- 'speech'：话术经验（发言/回复技巧），存量默认类别；
- 'decision'：决策经验（面对某种情景的广泛思路）。
写入与检索均按 category 定向：create/search/定位/删除支持 category 参数，
缺省 'speech' 保持存量调用方行为不变。

persona 列（2026-10-01）：人格绑定标签，''=全局通用，由反思经
upsert_bequest 的 persona 参数打标（观察期：仅落库，检索侧暂不消费）。
"""

from datetime import datetime
import re
from typing import List, Dict, Optional, Tuple

from .core import get_memory_db, serialize_vector, deserialize_vector, cosine_similarity
from .common import generate_timestamp_id
from .vector_store import get_vector_store
from utils.logger import get_logger

logger = get_logger(__name__, "DEBUG")


class BequestOperations:
    """嘱托/经验操作类（纯异步；两类经验：'speech' 话术 / 'decision' 决策）"""

    def __init__(self):
        self.db = get_memory_db()

    async def get_bequest_id_by_title(self, title: str, category: str = "speech") -> Optional[str]:
        """通过 title 精确查找经验（限定类别，speech/decision 各自独立命名空间）。"""
        if not title:
            return None
        conn = await self.db._ensure_connection()
        cur = await conn.execute(
            "SELECT bequest_id FROM bequests WHERE title=? AND category=? LIMIT 1",
            (title, category),
        )
        row = await cur.fetchone()
        return row["bequest_id"] if row else None

    async def search_bequests_by_title(
        self, query: str, category: str = "speech", limit: int = 3
    ) -> List[Dict]:
        """按标题比对经验（不生成向量，开销远小于向量检索）。

        命中规则：标题与查询完全相同者直接胜出（只返回该条）；否则按查询
        拆分出的关键词（空白与常见标点切分）逐个比对标题，命中关键词越多
        排名越靠前，同分取标题更短者。无任何关键词命中则返回空列表。
        """
        if not query or not query.strip():
            raise ValueError("查询文本不能为空")

        conn = await self.db._ensure_connection()
        cur = await conn.execute(
            "SELECT bequest_id, title, summary FROM bequests WHERE category=?",
            (category,),
        )
        rows = await cur.fetchall()

        keyword = query.strip()
        exact = [r for r in rows if r["title"] == keyword]
        if exact:
            rows = exact
            scored = [(len(keyword), r) for r in rows]
        else:
            tokens = [
                t for t in re.split(r"[\s，,。.；;、/|:：!！?？~～\-]+", keyword) if t
            ]
            scored = []
            for r in rows:
                hits = sum(1 for t in tokens if t in r["title"])
                if hits:
                    scored.append((hits, r))
            scored.sort(key=lambda item: (-item[0], len(item[1]["title"])))

        return [
            {
                "bequest_id": r["bequest_id"],
                "title": r["title"],
                "summary": r["summary"],
            }
            for _, r in scored[:limit]
        ]

    async def create_bequest(
        self, title: str, summary: str, category: str = "speech", persona: str = ""
    ) -> str:
        """创建经验（category 缺省 'speech'；persona 绑定人格标签，''=全局通用）。"""
        current_time = datetime.now().isoformat()
        bequest_id = generate_timestamp_id("Bequest")

        vector_store = get_vector_store()
        title_vector = await vector_store.generate_vector(title) if title else None

        conn = await self.db._ensure_connection()
        await conn.execute(
            """INSERT INTO bequests (bequest_id, title, summary, category, persona,
               title_vector, is_vectorized, start_time, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (bequest_id, title, summary, category, persona,
             serialize_vector(title_vector), 1 if title_vector else 0,
             current_time, current_time),
        )
        await conn.commit()
        return bequest_id

    async def update_bequest(self, title: str, category: str = "speech", **kwargs) -> bool:
        """更新经验（通过 title + category 定位）。"""
        bequest_id = await self.get_bequest_id_by_title(title, category=category)
        if not bequest_id:
            return False

        current_time = datetime.now().isoformat()
        allowed_fields = ['title', 'summary', 'persona']

        set_clauses = ["updated_at=?"]
        params = [current_time]

        for key, value in kwargs.items():
            if key in allowed_fields and value is not None:
                set_clauses.append(f"{key}=?")
                params.append(value)
                if key == 'title':
                    vector_store = get_vector_store()
                    title_vector = await vector_store.generate_vector(value) if value else None
                    set_clauses.append("title_vector=?")
                    params.append(serialize_vector(title_vector))
                    set_clauses.append("is_vectorized=?")
                    params.append(1 if title_vector else 0)

        params.append(bequest_id)
        conn = await self.db._ensure_connection()
        await conn.execute(
            f"UPDATE bequests SET {', '.join(set_clauses)} WHERE bequest_id=?",
            params,
        )
        await conn.commit()
        return True

    async def update_bequest_by_id(self, bequest_id: str, **kwargs) -> bool:
        """更新经验（通过 bequest_id 定位），供前端内联编辑使用。

        可改字段: title / summary（category 不开放修改，按原类别保留）。
        标题变更时重新生成 title_vector 并更新 is_vectorized。
        返回是否找到并成功更新。
        """
        if not bequest_id:
            return False

        conn = await self.db._ensure_connection()
        cur = await conn.execute(
            "SELECT bequest_id FROM bequests WHERE bequest_id=?", (bequest_id,)
        )
        if await cur.fetchone() is None:
            return False

        current_time = datetime.now().isoformat()
        allowed_fields = ["title", "summary"]

        set_clauses = ["updated_at=?"]
        params: List = [current_time]

        for key, value in kwargs.items():
            if key in allowed_fields and value is not None:
                set_clauses.append(f"{key}=?")
                params.append(value)
                if key == "title":
                    vector_store = get_vector_store()
                    title_vector = await vector_store.generate_vector(value) if value else None
                    set_clauses.append("title_vector=?")
                    params.append(serialize_vector(title_vector))
                    set_clauses.append("is_vectorized=?")
                    params.append(1 if title_vector else 0)

        params.append(bequest_id)
        await conn.execute(
            f"UPDATE bequests SET {', '.join(set_clauses)} WHERE bequest_id=?",
            params,
        )
        await conn.commit()
        return True

    async def delete_bequest_by_title(self, title: str, category: str = "speech") -> bool:
        """删除经验（通过 title + category 定位）。"""
        bequest_id = await self.get_bequest_id_by_title(title, category=category)
        if not bequest_id:
            return False
        conn = await self.db._ensure_connection()
        await conn.execute("DELETE FROM bequests WHERE bequest_id=?", (bequest_id,))
        await conn.commit()
        return True

    async def delete_bequest_by_id(self, bequest_id: str) -> bool:
        """删除经验（通过 bequest_id 定位，供前端内联删除使用）。"""
        if not bequest_id:
            return False
        conn = await self.db._ensure_connection()
        cur = await conn.execute(
            "SELECT bequest_id FROM bequests WHERE bequest_id=?", (bequest_id,)
        )
        if await cur.fetchone() is None:
            return False
        await conn.execute("DELETE FROM bequests WHERE bequest_id=?", (bequest_id,))
        await conn.commit()
        return True

    async def search_bequest_by_vector(
        self,
        query_text: str,
        top_k: int = 5,
        category: str = "speech",
        record_usage: bool = False,
    ) -> List[Tuple[str, float]]:
        """根据查询文本进行向量相似度搜索，返回最相似的 (bequest_id, 相似度) 列表。

        按 category 定向检索（speech/decision 互不串扰），缺省 'speech'
        保持存量调用方行为不变。
        返回分数供调用方落日志与后续阈值调参。
        注：persona 标签目前只落库（反思打标签观察期），检索侧暂不按其过滤。
        """
        if not query_text:
            raise ValueError("查询文本不能为空")

        vector_store = get_vector_store()
        query_vector = await vector_store.generate_vector(query_text)
        if not query_vector:
            return []

        conn = await self.db._ensure_connection()
        cur = await conn.execute(
            "SELECT bequest_id, title, title_vector FROM bequests "
            "WHERE is_vectorized=1 AND category=?",
            (category,),
        )
        rows = await cur.fetchall()

        results = []
        for r in rows:
            vec = deserialize_vector(r["title_vector"])
            if vec and len(vec) == len(query_vector):
                sim = cosine_similarity(query_vector, vec)
                results.append((r["bequest_id"], sim))

        results.sort(key=lambda x: x[1], reverse=True)
        selected = [(bequest_id, sim) for bequest_id, sim in results[:top_k]]
        if record_usage:
            from brain.hc.maintenance import MaintenanceService
            await MaintenanceService().record_usage("experience", [item[0] for item in selected], "vector_returned")
        return selected

    async def backfill_missing_vectors(self) -> int:
        """为所有缺失向量的经验补算 title_vector（一键回填存量数据）。

        仅处理 is_vectorized=0 且 title 非空的行，逐条生成并立即提交；
        嵌入调用失败直接上抛崩溃阻断（按 user_rules），已处理行不回滚，重跑可续。
        返回本次补算条数。
        """
        conn = await self.db._ensure_connection()
        cur = await conn.execute(
            "SELECT bequest_id, title FROM bequests WHERE is_vectorized=0 AND title != ''"
        )
        rows = await cur.fetchall()

        vector_store = get_vector_store()
        count = 0
        for r in rows:
            vector = await vector_store.generate_vector(r["title"], target="bequest")
            if not vector:
                raise RuntimeError(
                    "向量嵌入功能未启用（ENABLE_VECTOR_EMBEDDING=False），无法回填"
                )
            await conn.execute(
                "UPDATE bequests SET title_vector=?, is_vectorized=1 WHERE bequest_id=?",
                (serialize_vector(vector), r["bequest_id"]),
            )
            await conn.commit()
            count += 1
            logger.info(f"[向量回填] {r['bequest_id']} {r['title']!r} 维度={len(vector)}")
        return count

    async def regenerate_all_vectors(self) -> int:
        """清空并重建全部经验的 title_vector（嵌入模型切换/维度变更后一键重建）。

        逐条重算并原子覆写：旧向量保留到新向量成功生成为止（避免整体清空后
        API 失败导致全库裸奔）；title 为空的行直接置空向量。
        嵌入调用失败直接上抛崩溃阻断（按 user_rules），已处理行不回滚，重跑可续。
        返回重建条数（含置空的空标题行）。
        """
        conn = await self.db._ensure_connection()
        cur = await conn.execute("SELECT bequest_id, title FROM bequests")
        rows = await cur.fetchall()

        vector_store = get_vector_store()
        count = 0
        for r in rows:
            vector = (
                await vector_store.generate_vector(r["title"], target="bequest")
                if r["title"] else None
            )
            if r["title"] and not vector:
                raise RuntimeError(
                    "向量嵌入功能未启用（ENABLE_VECTOR_EMBEDDING=False），无法重建"
                )
            await conn.execute(
                "UPDATE bequests SET title_vector=?, is_vectorized=? WHERE bequest_id=?",
                (serialize_vector(vector), 1 if vector else 0, r["bequest_id"]),
            )
            await conn.commit()
            count += 1
            dim = len(vector) if vector else 0
            logger.info(f"[向量重建] {r['bequest_id']} {r['title']!r} 维度={dim}")
        return count

    async def get_bequest_by_id(self, bequest_id: str) -> Optional[Dict]:
        """获取单个经验的原始数据字典。"""
        if not bequest_id:
            return None
        conn = await self.db._ensure_connection()
        cur = await conn.execute(
            "SELECT bequest_id, title, summary, category, start_time FROM bequests WHERE bequest_id=?",
            (bequest_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return {
            "bequest_id": row["bequest_id"],
            "title": row["title"],
            "summary": row["summary"],
            "category": row["category"],
            "start_time": row["start_time"],
        }

    async def clear_database(self):
        """清空所有经验。"""
        conn = await self.db._ensure_connection()
        await conn.execute("DELETE FROM bequests")
        await conn.commit()
        print("嘱托数据库已清空！")
