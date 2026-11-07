"""人工维护的锦囊条目；只保存建议正文、作用困境、困境分类和自动元数据。"""
from datetime import datetime
from uuid import uuid4

from .core import get_memory_db


class DecisionKitOperations:
    FIELDS = ("advice", "dilemma", "category")

    def __init__(self):
        self.db = get_memory_db()

    @classmethod
    def validate(cls, data, partial=False):
        if not isinstance(data, dict):
            raise ValueError("必须提交 JSON 对象")
        unknown = set(data) - set(cls.FIELDS)
        if unknown:
            raise ValueError("未知字段：" + "、".join(sorted(unknown)))
        if not data or (not partial and set(data) != set(cls.FIELDS)):
            raise ValueError("请填写建议正文、作用困境和困境分类")
        result = {}
        for key, value in data.items():
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{key} 必须是非空文字")
            result[key] = value.strip()
        return result

    async def list_all(self):
        conn = await self.db._ensure_connection()
        cursor = await conn.execute("SELECT * FROM decision_kits ORDER BY created_at, kit_id")
        return [dict(row) for row in await cursor.fetchall()]

    async def get(self, kit_id):
        conn = await self.db._ensure_connection()
        cursor = await conn.execute("SELECT * FROM decision_kits WHERE kit_id=?", (kit_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def create(self, data):
        fields = self.validate(data)
        kit_id = "Kit_" + uuid4().hex
        now = datetime.now().isoformat(timespec="milliseconds")
        conn = await self.db._ensure_connection()
        await conn.execute(
            "INSERT INTO decision_kits VALUES (?, ?, ?, ?, ?, ?)",
            (kit_id, fields["advice"], fields["dilemma"], fields["category"], now, now),
        )
        await conn.commit()
        return await self.get(kit_id)

    async def update(self, kit_id, data):
        fields = self.validate(data, partial=True)
        conn = await self.db._ensure_connection()
        columns = [f"{field}=?" for field in fields]
        cursor = await conn.execute(
            f"UPDATE decision_kits SET {', '.join(columns)}, updated_at=? WHERE kit_id=?",
            (*fields.values(), datetime.now().isoformat(timespec="milliseconds"), kit_id),
        )
        await conn.commit()
        return await self.get(kit_id) if cursor.rowcount else None

    async def delete(self, kit_id):
        conn = await self.db._ensure_connection()
        cursor = await conn.execute("DELETE FROM decision_kits WHERE kit_id=?", (kit_id,))
        await conn.commit()
        return bool(cursor.rowcount)
