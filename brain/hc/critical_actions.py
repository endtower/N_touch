"""四种临界行动的唯一登记入口；无成功次数门槛、补偿或旧维护审批链。"""
import asyncio
import json
import time
from uuid import uuid4

from brain.amy.feedback_state import get_feedback_state
from brain.amy.failure_reasons import failure_reason_lock, pending_failure_reasons
from brain.hc.database.core import get_memory_db
from config import maintenance_settings as settings

ACTION_STATES = {
    "申请维护": ("competence", "competence_value", "high"),
    "尝试自修复": ("competence", "competence_value", "low"),
    "情绪宣泄": ("recognition", "value", "low"),
    "发布关系改进任务": ("recognition", "value", "high"),
}
_action_lock = asyncio.Lock()


def available_actions(state):
    return [name for name, (_, key, edge) in ACTION_STATES.items()
            if (state[key] >= settings.HIGH_THRESHOLD if edge == "high" else state[key] <= settings.LOW_THRESHOLD)]


class CriticalActions:
    async def recover_resets(self):
        conn = await get_memory_db()._ensure_connection()
        rows = await (await conn.execute("SELECT * FROM critical_actions WHERE state='registered' AND reset_applied=0")).fetchall()
        for row in rows:
            details = json.loads(row["details_json"])
            get_feedback_state().reset_for_critical_action(row["action_id"], row["axis"], row["title"], details["reason"])
            await conn.execute("UPDATE critical_actions SET reset_applied=1 WHERE action_id=?", (row["action_id"],))
            await conn.commit()

    async def register(self, action, title, reason, plan, next_occurrence, notify_master="",
                       verification="", event="", target="", expectation=""):
        from brain.base.common_ways.tool_task import ToolTask
        from brain.base.light_agent.quest_agent import QuestAgent

        if action not in ACTION_STATES:
            raise ValueError("请选择统一维护工具列出的四种行动")
        details = dict(reason=reason, plan=plan, next_occurrence=next_occurrence,
                       notify_master=notify_master, verification=verification, event=event,
                       target=target, expectation=expectation)
        for key, value in {"title": title, **details}.items():
            if not isinstance(value, str):
                raise ValueError(f"{key} 必须为文字")
        required = ["reason", "plan", "next_occurrence"] + {
            "申请维护": ["notify_master"], "尝试自修复": ["verification"],
            "情绪宣泄": ["event", "target"], "发布关系改进任务": ["target", "expectation"],
        }[action]
        if not title.strip() or any(not details[key].strip() for key in required):
            raise ValueError("请填写标题及本类行动要求的原因、实施内容、时间和专用信息")
        details = {key: value.strip() for key, value in details.items()}
        details["next_occurrence"] = QuestAgent._resolve_next_occurrence(next_occurrence)
        async with _action_lock:
            await self.recover_resets()
            state = get_feedback_state().snapshot()
            if action not in available_actions(state):
                raise ValueError("当前情感不符合该行动：效能感100申请维护、为0尝试自修复；归属感为0情绪宣泄、100发布关系改进任务")
            axis = ACTION_STATES[action][0]
            action_id = uuid4().hex
            conn = await get_memory_db()._ensure_connection()
            task_ids = []
            if action != "申请维护":
                marker = f"【统一维护行动编号：{action_id}】"
                guidance = {
                    "尝试自修复": "将自身错误、预期修改方向和实践验证方法完整保留；到时实施尝试并核对实际结果。登记不代表验证有效。",
                    "情绪宣泄": "保留宣泄事件、对象、不满缘由和表达打算；在指定时间交付 PFCA，根据当时情境实施。",
                    "发布关系改进任务": "保留关系目标、对象、期望与改进办法；在指定时间交付 PFCA，根据实际回应判断进展。",
                }[action]
                context = marker + f"\n【类型：{action}】\n" + guidance + "\n" + json.dumps(details, ensure_ascii=False)
                result = await ToolTask._run_quest_request(title.strip(), context, details["next_occurrence"])
                candidates = (result.get("created_tasks", []) + result.get("updated_tasks", [])) if isinstance(result, dict) else []
                # 只承认数据库中已存在的待办，不承认 Quest 的自然语言成功宣称。
                for item in candidates:
                    task_id = item.get("task_id")
                    row = await (await conn.execute("SELECT task_id FROM tasks WHERE task_id=? AND status='active' AND instr(context,?)>0", (task_id, marker))).fetchone()
                    if row:
                        task_ids.append(row["task_id"])
                if not task_ids:
                    return {"status": "not_registered", "message": "Quest 未实际登记待办，情感值未重置", "quest_result": result}
            async with failure_reason_lock:
                reasons = await pending_failure_reasons() if action == "申请维护" else []
                try:
                    await conn.execute("""INSERT INTO critical_actions
                        (action_id,action,axis,title,details_json,task_ids_json,issues_json,state,created_at)
                        VALUES (?,?,?,?,?,?,?,'registered',?)""",
                        (action_id, action, axis, title.strip(), json.dumps(details, ensure_ascii=False),
                         json.dumps(task_ids), json.dumps(reasons, ensure_ascii=False), time.time()))
                    if action == "申请维护":
                        await conn.executemany("DELETE FROM failure_reason_cache WHERE reason_id=?", [(r["reason_id"],) for r in reasons])
                    await conn.commit()
                except BaseException:
                    await conn.rollback()
                    raise
            await self.recover_resets()
            return {"status": "registered", "action_id": action_id, "action": action,
                    "task_ids": task_ids, "reset_axis": axis, "reset_value": settings.RESET_VALUE,
                    "failure_reasons": reasons,
                    "message": ("维护申请已发布，本批 failure_reason 已归入申请并清空待收集缓存。请按提醒方案向主人说明维护需求和实施时间。"
                                if action == "申请维护" else "对应待办已由 Quest 登记，对应情感值已重置为50。")}
