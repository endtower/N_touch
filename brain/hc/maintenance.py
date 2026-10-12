"""人工维护台账、使用机会统计和高值行动登记。内容仍保留在原有存储中。"""
import asyncio
import hashlib
import json
import time
from uuid import uuid4

from brain.hc.database.core import get_memory_db
from brain.amy.feedback_state import get_feedback_state
from config import maintenance_settings as settings
from utils.chain_context import get_current_chain_id

_write_lock = asyncio.Lock()
KINDS = ("experience", "persona", "kit")


def revision_of(content):
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class MaintenanceService:
    async def _conn(self):
        return await get_memory_db()._ensure_connection()

    async def items(self, kind=None):
        if kind is not None and kind not in KINDS:
            raise ValueError("内容类型必须为 experience、persona 或 kit")
        conn = await self._conn()
        result = []
        for category in ([kind] if kind else KINDS):
            if category == "experience":
                rows = await (await conn.execute("SELECT bequest_id, title, summary, category, persona FROM bequests")).fetchall()
                source = [(r["bequest_id"], r["title"], dict(r)) for r in rows]
            elif category == "kit":
                rows = await (await conn.execute("SELECT kit_id, advice, dilemma, category FROM decision_kits")).fetchall()
                source = [(r["kit_id"], r["dilemma"], dict(r)) for r in rows]
            else:
                from brain.base.persona.persona_registry import list_personas
                source = [(p.persona_name, p.persona_name, p.to_dict()) for p in list_personas()]
            for item_id, title, content in source:
                result.append({"kind": category, "item_id": item_id, "title": title,
                               "revision": revision_of(content), "content": content})
        return result

    async def item(self, kind, item_id):
        if kind not in KINDS or not isinstance(item_id, str) or not item_id:
            raise ValueError("必须指定有效内容类型和编号")
        return next((i for i in await self.items(kind) if i["item_id"] == item_id), None)

    async def metadata(self, kind=None):
        conn = await self._conn()
        reviews = await (await conn.execute("SELECT * FROM maintenance_reviews ORDER BY reviewed_at DESC")).fetchall()
        latest = {}
        for row in reviews:
            latest.setdefault((row["kind"], row["item_id"]), dict(row))
        usage = await (await conn.execute("SELECT kind,item_id,event_type,count(*) AS count,max(used_at) AS last_used_at FROM memory_usage_events GROUP BY kind,item_id,event_type")).fetchall()
        counts = {}
        for row in usage:
            counts.setdefault((row["kind"], row["item_id"]), {})[row["event_type"]] = {"count": row["count"], "last_used_at": row["last_used_at"]}
        items = await self.items(kind)
        for item in items:
            key = item["kind"], item["item_id"]
            review = latest.get(key)
            item["maintenance"] = {"last_review": review, "human_confirmed": bool(review and review["revision"] == item["revision"]),
                                   "changed_since_review": bool(review and review["revision"] != item["revision"])}
            item["usage"] = counts.get(key, {})
        return items

    async def review(self, kind, item_id, expected_revision, topic="日常维护", action="review"):
        if not isinstance(topic, str) or action not in ("review", "edit"):
            raise ValueError("人工维护专题或操作类型无效")
        async with _write_lock:
            item = await self.item(kind, item_id)
            if item is None:
                raise ValueError("内容不存在，可能已被删除")
            if expected_revision != item["revision"]:
                raise ValueError("内容已变更，请刷新后重新检查")
            conn = await self._conn()
            await conn.execute("INSERT INTO maintenance_reviews VALUES (?,?,?,?,?,?,?,?)",
                               (uuid4().hex, kind, item_id, item["title"], item["revision"], topic.strip() or "日常维护", action, time.time()))
            await conn.commit()
        return item

    async def record_usage(self, kind, item_ids, event_type, scope=None):
        if not item_ids:
            return
        allowed = {"experience": ("vector_returned", "returned", "injected"), "persona": ("selected",), "kit": ("returned",)}
        if kind not in allowed or event_type not in allowed[kind]:
            raise ValueError("使用统计类型不匹配")
        scope = scope or get_current_chain_id() or f"unlinked_{uuid4().hex}"
        items = {i["item_id"]: i for i in await self.items(kind)}
        async with _write_lock:
            conn = await self._conn()
            await conn.executemany("INSERT OR IGNORE INTO memory_usage_events VALUES (?,?,?,?,?,?)",
                                   [(kind, i, event_type, scope, items[i]["revision"], time.time()) for i in set(item_ids) if i in items])
            await conn.commit()

    async def recover_effects(self):
        """先登记，后重置；故障重启后补齐未完成结算，AMY 按 action_id 防重复。"""
        conn = await self._conn()
        rows = await (await conn.execute("SELECT * FROM growth_actions WHERE effect_applied=0 ORDER BY created_at")).fetchall()
        for row in rows:
            get_feedback_state().reset_for_growth(row["action_id"], row["axis"], row["title"], row["reason"])
            await conn.execute("UPDATE growth_actions SET effect_applied=1 WHERE action_id=?", (row["action_id"],))
        await conn.commit()

    async def eligibility(self):
        conn = await self._conn()
        state = get_feedback_state().snapshot()
        recent = state["task_results"][-settings.RECENT_TASK_WINDOW:]
        successes = sum(r["result"] == "成功" for r in recent)
        requests = await (await conn.execute("SELECT * FROM maintenance_requests ORDER BY created_at DESC")).fetchall()
        now = time.time()
        pending = any(r["status"] in ("pending", "in_progress") or (r["status"] == "deferred" and r["defer_until"] > now) for r in requests)
        # 暂缓到期后重新显示同一申请，不再制造一条新申请。
        resumable = any(r["status"] == "deferred" for r in requests)
        cooldown_until = (requests[0]["created_at"] + settings.REQUEST_INTERVAL_HOURS * 3600) if requests else 0
        maintenance_reason = ""
        if state["competence_value"] < settings.HIGH_THRESHOLD:
            maintenance_reason = f"效能感未达到 {settings.HIGH_THRESHOLD}"
        elif successes < settings.MIN_TASK_SUCCESSES:
            maintenance_reason = f"最近 {settings.RECENT_TASK_WINDOW} 次明确任务结果不足 {settings.MIN_TASK_SUCCESSES} 次成功"
        elif pending or resumable:
            maintenance_reason = "已有待处理、进行中或暂缓的维护申请，请处理原申请"
        elif cooldown_until > now:
            maintenance_reason = "维护申请间隔尚未结束"
        social_reason = "" if state["value"] >= settings.HIGH_THRESHOLD else f"归属感未达到 {settings.HIGH_THRESHOLD}"
        rows = await (await conn.execute("SELECT reference_json FROM growth_actions WHERE axis='recognition'")).fetchall()
        task_ids = [i for r in rows for i in json.loads(r["reference_json"])]
        if not social_reason and task_ids:
            marks = ",".join("?" for _ in task_ids)
            active = await (await conn.execute(f"SELECT count(*) FROM tasks WHERE task_id IN ({marks}) AND status='active'", task_ids)).fetchone()
            if active[0]:
                social_reason = "已有未完成的关系建设任务"
        return {"maintenance": {"allowed": not maintenance_reason, "reason": maintenance_reason, "cooldown_until": cooldown_until},
                "social_growth": {"allowed": not social_reason, "reason": social_reason},
                "recent_tasks": recent, "high_threshold": settings.HIGH_THRESHOLD}

    async def query(self, topic=""):
        async with _write_lock:
            await self.recover_effects()
        conn = await self._conn()
        # 未处理申请始终可见，不能因历史记录增加而被最近记录窗口挤掉。
        requests = [dict(r) for r in await (await conn.execute("SELECT * FROM maintenance_requests WHERE status IN ('pending','in_progress','deferred') OR request_id IN (SELECT request_id FROM maintenance_requests ORDER BY created_at DESC LIMIT 20) ORDER BY created_at DESC")).fetchall()]
        reviews = [dict(r) for r in await (await conn.execute("SELECT * FROM maintenance_reviews ORDER BY reviewed_at DESC LIMIT 50")).fetchall()]
        for request in requests:
            request["items"] = json.loads(request.pop("items_json"))
        if topic:
            requests = [r for r in requests if topic.casefold() in r["topic"].casefold()]
            reviews = [r for r in reviews if topic.casefold() in r["topic"].casefold()]
        return {"requests": requests, "reviews": reviews[:20], "eligibility": await self.eligibility()}

    async def request(self, topic, reason, items):
        if not isinstance(topic, str) or not topic.strip() or len(topic) > 80 or not isinstance(reason, str) or not reason.strip():
            raise ValueError("请提供一个具体专题（最多80字）及维护依据")
        if not isinstance(items, list) or not 1 <= len(items) <= 5:
            raise ValueError("每次围绕一个专题选择1至5条内容")
        async with _write_lock:
            await self.recover_effects()
            eligibility = (await self.eligibility())["maintenance"]
            if not eligibility["allowed"]:
                return {"status": "blocked", **eligibility}
            resolved = []
            for ref in items:
                if not isinstance(ref, dict) or set(ref) != {"kind", "item_id"}:
                    raise ValueError("每条内容必须指定 kind 和 item_id")
                item = await self.item(ref["kind"], ref["item_id"])
                if item is None:
                    raise ValueError("申请中的内容不存在")
                resolved.append({k: item[k] for k in ("kind", "item_id", "title", "revision")})
            request_id, now = uuid4().hex, time.time()
            conn = await self._conn()
            try:
                await conn.execute("INSERT INTO maintenance_requests (request_id,topic,reason,items_json,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                                   (request_id, topic.strip(), reason.strip(), json.dumps(resolved, ensure_ascii=False), now, now))
                await conn.execute("INSERT INTO growth_actions VALUES (?,?,?,?,?,?,0)",
                                   (f"maintenance:{request_id}", "competence", json.dumps([request_id]), "已申请专题维护", reason.strip(), now))
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
            await self.recover_effects()
        return {"status": "registered", "request_id": request_id, "topic": topic.strip(), "reset_axis": "效能感", "reset_value": settings.RESET_VALUE,
                "hint": "申请已显示在记忆页的维护栏目。等待主人处理，不要重复申请；登记不表示主人已同意修改。"}

    async def handle_request(self, request_id, status, conclusion="", defer_hours=24):
        if status not in ("in_progress", "completed", "unchanged", "deferred", "rejected"):
            raise ValueError("维护处理状态无效")
        if not isinstance(conclusion, str):
            raise ValueError("处理说明必须为文字")
        if status == "deferred" and (not isinstance(defer_hours, (int, float)) or not 1 <= defer_hours <= 8760):
            raise ValueError("暂缓时间应为1至8760小时")
        async with _write_lock:
            conn = await self._conn()
            row = await (await conn.execute("SELECT * FROM maintenance_requests WHERE request_id=?", (request_id,))).fetchone()
            if row is None:
                raise ValueError("维护申请不存在")
            if row["status"] in ("completed", "unchanged", "rejected"):
                raise ValueError("该申请已处理完毕")
            now = time.time()
            unconfirmed = []
            try:
                # “无需修改”只确认申请时的版本；内容已变化则提示主人重新检查。
                if status == "unchanged":
                    for ref in json.loads(row["items_json"]):
                        current = await self.item(ref["kind"], ref["item_id"])
                        if current and current["revision"] == ref["revision"]:
                            await conn.execute("INSERT INTO maintenance_reviews VALUES (?,?,?,?,?,?,?,?)",
                                               (uuid4().hex, ref["kind"], ref["item_id"], current["title"], ref["revision"], row["topic"], "review", now))
                        else:
                            unconfirmed.append(ref)
                await conn.execute("UPDATE maintenance_requests SET status=?,updated_at=?,defer_until=?,conclusion=? WHERE request_id=?",
                                   (status, now, now + defer_hours * 3600 if status == "deferred" else 0, conclusion.strip(), request_id))
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return {"status": status, "unconfirmed_items": unconfirmed}

    async def register_social_growth(self, created_tasks, reason):
        if not created_tasks:
            return {"registered": False, "reason": "Quest 未创建关系建设任务，不重置归属感"}
        ids = sorted({t["task_id"] for t in created_tasks})
        async with _write_lock:
            conn = await self._conn()
            action_id = "social_growth:" + revision_of(ids)
            existing = await (await conn.execute("SELECT action_id FROM growth_actions WHERE action_id=?", (action_id,))).fetchone()
            if existing:
                await self.recover_effects()
                return {"registered": False, "duplicate": True, "reason": "该组任务已经结算，不重复重置"}
            eligibility = (await self.eligibility())["social_growth"]
            if not eligibility["allowed"]:
                return {"registered": False, **eligibility}
            for task_id in ids:
                row = await (await conn.execute("SELECT task_id FROM tasks WHERE task_id=?", (task_id,))).fetchone()
                if row is None:
                    raise ValueError("关系建设任务未实际落库")
            await conn.execute("INSERT OR IGNORE INTO growth_actions VALUES (?,?,?,?,?,?,0)",
                               (action_id, "recognition", json.dumps(ids), "已安排关系建设", reason, time.time()))
            await conn.commit()
            await self.recover_effects()
        return {"registered": True, "reset_axis": "归属感", "reset_value": settings.RESET_VALUE, "task_ids": ids}
