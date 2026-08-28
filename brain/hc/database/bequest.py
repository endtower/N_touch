# -*- coding: utf-8 -*-
"""
嘱托/经验操作类（SQLite）

经验是独立表，记录长期经验/嘱托（decision / execution 场景）。
"""

from datetime import datetime
from typing import List, Dict, Optional

from .core import get_memory_db, serialize_vector, deserialize_vector, cosine_similarity
from .common import generate_timestamp_id
from .vector_store import get_vector_store
from utils.logger import get_logger

logger = get_logger(__name__, "DEBUG")


# 向量检索黑名单：在向量比对阶段直接排除的默认经验标题
EXCLUDED_TITLES: List[str] = [
    "线上交流的基础方法",
]


class BequestOperations:
    """嘱托/经验操作类（纯异步）"""

    def __init__(self):
        self.db = get_memory_db()

    async def get_bequest_id_by_title(self, title: str) -> Optional[str]:
        """通过 title 精确查找经验。"""
        if not title:
            return None
        conn = await self.db._ensure_connection()
        cur = await conn.execute("SELECT bequest_id FROM bequests WHERE title=? LIMIT 1", (title,))
        row = await cur.fetchone()
        return row["bequest_id"] if row else None

    async def create_bequest(self, title: str, summary: str, apply_scenario: Optional[str] = None) -> str:
        """创建经验。"""
        current_time = datetime.now().isoformat()
        bequest_id = generate_timestamp_id("Bequest")

        vector_store = get_vector_store()
        title_vector = vector_store.generate_vector(title) if title else None

        conn = await self.db._ensure_connection()
        await conn.execute(
            """INSERT INTO bequests (bequest_id, title, summary, apply_scenario,
               title_vector, is_vectorized, start_time, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (bequest_id, title, summary, apply_scenario or "",
             serialize_vector(title_vector), 1 if title_vector else 0,
             current_time, current_time),
        )
        await conn.commit()
        return bequest_id

    async def update_bequest(self, title: str, **kwargs) -> bool:
        """更新经验（通过 title 定位）。"""
        bequest_id = await self.get_bequest_id_by_title(title)
        if not bequest_id:
            return False

        current_time = datetime.now().isoformat()
        allowed_fields = ['title', 'summary', 'apply_scenario']

        set_clauses = ["updated_at=?"]
        params = [current_time]

        for key, value in kwargs.items():
            if key in allowed_fields and value is not None:
                set_clauses.append(f"{key}=?")
                params.append(value)
                if key == 'title':
                    vector_store = get_vector_store()
                    title_vector = vector_store.generate_vector(value) if value else None
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

        可改字段: title / summary / apply_scenario。
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
        allowed_fields = ["title", "summary", "apply_scenario"]

        set_clauses = ["updated_at=?"]
        params: List = [current_time]

        for key, value in kwargs.items():
            if key in allowed_fields and value is not None:
                set_clauses.append(f"{key}=?")
                params.append(value)
                if key == "title":
                    vector_store = get_vector_store()
                    title_vector = vector_store.generate_vector(value) if value else None
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

    async def delete_bequest_by_title(self, title: str) -> bool:
        """删除经验（通过 title 定位）。"""
        bequest_id = await self.get_bequest_id_by_title(title)
        if not bequest_id:
            return False
        conn = await self.db._ensure_connection()
        await conn.execute("DELETE FROM bequests WHERE bequest_id=?", (bequest_id,))
        await conn.commit()
        return True

    async def search_bequest_by_vector(
        self,
        query_text: str,
        top_k: int = 5,
        exclude_titles: Optional[List[str]] = None,
    ) -> List[str]:
        """根据查询文本进行向量相似度搜索，返回最相似的 bequest_id 列表。"""
        if not query_text:
            raise ValueError("查询文本不能为空")

        if exclude_titles is None:
            exclude_titles = EXCLUDED_TITLES

        vector_store = get_vector_store()
        query_vector = vector_store.generate_vector(query_text)
        if not query_vector:
            return []

        conn = await self.db._ensure_connection()
        cur = await conn.execute(
            "SELECT bequest_id, title, title_vector FROM bequests WHERE is_vectorized=1"
        )
        rows = await cur.fetchall()

        results = []
        for r in rows:
            if r["title"] in exclude_titles:
                continue
            vec = deserialize_vector(r["title_vector"])
            if vec and len(vec) == len(query_vector):
                sim = cosine_similarity(query_vector, vec)
                results.append((r["bequest_id"], sim))

        results.sort(key=lambda x: x[1], reverse=True)
        return [bequest_id for bequest_id, _ in results[:top_k]]

    async def search_bequest_by_scenario(self, scenario_keyword: str) -> List[str]:
        """根据应用场景关键词搜索经验。"""
        if not scenario_keyword:
            return []
        conn = await self.db._ensure_connection()
        cur = await conn.execute(
            "SELECT bequest_id FROM bequests WHERE apply_scenario LIKE ? ORDER BY start_time DESC",
            (f"%{scenario_keyword}%",),
        )
        rows = await cur.fetchall()
        return [r["bequest_id"] for r in rows if r["bequest_id"]]

    async def get_bequest_by_id(self, bequest_id: str) -> Optional[Dict]:
        """获取单个经验的原始数据字典。"""
        if not bequest_id:
            return None
        conn = await self.db._ensure_connection()
        cur = await conn.execute(
            "SELECT bequest_id, title, summary, apply_scenario, start_time FROM bequests WHERE bequest_id=?",
            (bequest_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return {
            "bequest_id": row["bequest_id"],
            "title": row["title"],
            "summary": row["summary"],
            "apply_scenario": row["apply_scenario"],
            "start_time": row["start_time"],
        }

    async def format_bequest_by_id(self, bequest_id: str) -> str:
        """格式化单个经验信息（合并 format_bequest_by_title）。"""
        if not bequest_id:
            return "未指定嘱托ID"
        bequest = await self.get_bequest_by_id(bequest_id)
        if not bequest:
            return f"未找到嘱托ID为「{bequest_id}」的嘱托"
        return self._format_bequest(bequest)

    async def format_bequest_by_title(self, title: str) -> str:
        """按标题格式化经验信息。"""
        if not title:
            return "未指定嘱托标题"
        bequest_id = await self.get_bequest_id_by_title(title)
        if not bequest_id:
            return f"未找到标题为「{title}」的嘱托"
        bequest = await self.get_bequest_by_id(bequest_id)
        return self._format_bequest(bequest)

    def _format_bequest(self, bequest: Dict) -> str:
        """格式化单个经验字典。"""
        formatted_output = []
        formatted_output.append(f"名称：{bequest.get('title', '未知标题')}")
        formatted_output.append(f"嘱托内容：{bequest.get('summary', '无')}")

        apply_scenario = bequest.get("apply_scenario", "")
        if apply_scenario:
            formatted_output.append(f"应用场景：{apply_scenario}")

        start_time = bequest.get("start_time", "")
        if start_time:
            try:
                formatted_output.append(
                    f"创建时间：{datetime.fromisoformat(start_time).strftime('%Y-%m-%d %H:%M')}"
                )
            except ValueError:
                pass

        return "\n".join(formatted_output)

    async def get_all_bequests(self, limit: int = 50) -> List[Dict]:
        """获取所有经验。"""
        conn = await self.db._ensure_connection()
        cur = await conn.execute(
            "SELECT bequest_id, title, summary, apply_scenario, start_time "
            "FROM bequests ORDER BY start_time DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        return [
            {
                "bequest_id": r["bequest_id"],
                "title": r["title"],
                "summary": r["summary"],
                "apply_scenario": r["apply_scenario"],
                "start_time": r["start_time"],
            }
            for r in rows
        ]

    async def get_bequests_by_scenario(self, apply_scenario: str, limit: int = 50) -> List[Dict]:
        """根据应用场景类型获取经验列表。"""
        conn = await self.db._ensure_connection()
        cur = await conn.execute(
            "SELECT bequest_id, title, summary, apply_scenario, start_time "
            "FROM bequests WHERE apply_scenario=? ORDER BY start_time DESC LIMIT ?",
            (apply_scenario, limit),
        )
        rows = await cur.fetchall()
        return [
            {
                "bequest_id": r["bequest_id"],
                "title": r["title"],
                "summary": r["summary"],
                "apply_scenario": r["apply_scenario"],
                "start_time": r["start_time"],
            }
            for r in rows
        ]

    async def format_bequests_by_scenario(self, apply_scenario: str = "all", limit: int = 50, show_header: bool = True) -> str:
        """根据应用场景类型获取并格式化经验列表。"""
        if apply_scenario == "all":
            return await self.format_all_bequests_grouped(limit)

        bequests = await self.get_bequests_by_scenario(apply_scenario, limit)
        if not bequests:
            return f"（无 {apply_scenario} 类型的经验嘱托）"

        lines = []
        if show_header:
            scenario_name = "决策阶段" if apply_scenario == "decision" else "执行阶段" if apply_scenario == "execution" else apply_scenario
            lines.append(f"【{scenario_name} 经验/嘱托】共 {len(bequests)} 条：")

        for i, bequest in enumerate(bequests, 1):
            title = bequest.get("title", "未命名")
            summary = bequest.get("summary", "无内容")
            lines.append(f"{i}. {title}：{summary}")

        return "\n".join(lines)

    async def format_all_bequests_grouped(self, limit: int = 50) -> str:
        """获取并按类型分组格式化所有经验。"""
        all_bequests = await self.get_all_bequests(limit)

        decision_bequests = [b for b in all_bequests if b.get("apply_scenario") == "decision"]
        execution_bequests = [b for b in all_bequests if b.get("apply_scenario") == "execution"]
        other_bequests = [b for b in all_bequests if b.get("apply_scenario") not in ["decision", "execution"]]

        lines = []
        if decision_bequests:
            lines.append(f"【决策阶段 经验/嘱托】共 {len(decision_bequests)} 条（上限 5 条）：")
            for i, bequest in enumerate(decision_bequests, 1):
                lines.append(f"  {i}. {bequest.get('title', '未命名')}：{bequest.get('summary', '无内容')}")

        if execution_bequests:
            lines.append(f"\n【执行阶段 经验/嘱托】共 {len(execution_bequests)} 条（上限 5 条）：")
            for i, bequest in enumerate(execution_bequests, 1):
                lines.append(f"  {i}. {bequest.get('title', '未命名')}：{bequest.get('summary', '无内容')}")

        if other_bequests:
            lines.append(f"\n【其他 经验/嘱托】共 {len(other_bequests)} 条：")
            for i, bequest in enumerate(other_bequests, 1):
                scenario = bequest.get("apply_scenario", "未指定")
                lines.append(f"  {i}. [{scenario}] {bequest.get('title', '未命名')}：{bequest.get('summary', '无内容')}")

        if not lines:
            return "（无已登记的经验嘱托）"

        lines.append(f"\n【总计】{len(all_bequests)} 条（上限 10 条）")
        return "\n".join(lines)

    async def clear_database(self):
        """清空所有经验。"""
        conn = await self.db._ensure_connection()
        await conn.execute("DELETE FROM bequests")
        await conn.commit()
        print("嘱托数据库已清空！")
