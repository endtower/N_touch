"""经验的人工维护统计；不发放情感补偿，不审批行动。"""
import json
import time

from brain.hc.database.core import get_memory_db
from brain.amy.failure_reasons import pending_failure_reasons


class MaintenanceService:
    async def metadata(self, kind=None):
        if kind not in (None, "experience"):
            return []
        conn = await get_memory_db()._ensure_connection()
        rows = await (await conn.execute("""SELECT bequest_id AS item_id,title,retrieval_count,
            last_maintained_at,manual_maintenance_count FROM bequests ORDER BY title""")).fetchall()
        return [{"kind": "experience", **dict(r)} for r in rows]

    async def review(self, item_id):
        conn = await get_memory_db()._ensure_connection()
        result = await conn.execute("""UPDATE bequests SET last_maintained_at=?,
            manual_maintenance_count=manual_maintenance_count+1 WHERE bequest_id=?""", (time.time(), item_id))
        if result.rowcount != 1:
            raise ValueError("经验不存在")
        await conn.commit()

    async def review_all(self):
        """把所有经验各记录一次人工维护，返回更新的经验条数。"""
        conn = await get_memory_db()._ensure_connection()
        result = await conn.execute(
            "UPDATE bequests SET last_maintained_at=?, manual_maintenance_count=manual_maintenance_count+1",
            (time.time(),),
        )
        await conn.commit()
        return result.rowcount

    async def query(self):
        conn = await get_memory_db()._ensure_connection()
        # 活跃与已归档各取一段：归档不能把活跃申请挤出列表。
        rows = await (await conn.execute("""SELECT * FROM critical_actions
            WHERE action='申请维护' AND state='registered'
            ORDER BY archived_at IS NOT NULL, created_at DESC LIMIT 200""")).fetchall()
        requests = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            item["failure_reasons"] = json.loads(item.pop("issues_json"))
            item["task_ids"] = json.loads(item.pop("task_ids_json"))
            requests.append(item)
        return {"requests": requests, "failure_reasons": await pending_failure_reasons()}

    async def archive(self, action_id, archived=True):
        """归档或恢复一条维护记录；只改归档时间，不动 state 与重置台账。"""
        conn = await get_memory_db()._ensure_connection()
        result = await conn.execute(
            "UPDATE critical_actions SET archived_at=? WHERE action_id=? AND action='申请维护'",
            (time.time() if archived else None, action_id),
        )
        if result.rowcount != 1:
            raise ValueError("维护申请不存在")
        await conn.commit()
