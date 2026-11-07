"""四种临界行动的唯一登记入口；无成功次数门槛、补偿或旧维护审批链。"""
import asyncio
import json
import time
from datetime import datetime
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
    @staticmethod
    def _task_context(action, title, details):
        """把维护行动的细节写成 PFCA 下次启动用的启动单（目的 / 要做 / 对象 / 依据 / 兜底）。"""
        purpose = {
            "尝试自修复": "把自己的错误改过来，并把改法验证一次",
            "情绪宣泄": "把眼下的不满和心情直接表达出去，让自己好受一点",
            "发布关系改进任务": "把与对方的关系往想要的方向推进一层",
        }[action]
        first_step = {
            "尝试自修复": details["plan"],
            "情绪宣泄": f"{details['plan']}（对象：{details['target']}；起因：{details['event']}）",
            "发布关系改进任务": f"{details['plan']}（对象：{details['target']}；期望：{details['expectation']}）",
        }[action]
        lines = [f"【类型：{action}】{title}", f"目的：{purpose}", f"要做：{first_step}",
                 f"依据：{details['reason']}",
                 "兜底：对方无回应时换个渠道直接再说一次，或明说自己的心情求助，不要静默收场"]
        return "\n".join(lines)

    async def recover_resets(self):
        conn = await get_memory_db()._ensure_connection()
        rows = await (await conn.execute("SELECT * FROM critical_actions WHERE state='registered' AND reset_applied=0")).fetchall()
        for row in rows:
            details = json.loads(row["details_json"])
            get_feedback_state().reset_for_critical_action(row["action_id"], row["axis"], row["title"], details["reason"])
            await conn.execute("UPDATE critical_actions SET reset_applied=1 WHERE action_id=?", (row["action_id"],))
            await conn.commit()

    async def register(self, action, title, reason, plan, notify_master="",
                       verification="", event="", target="", expectation=""):
        from brain.hc.database.common import generate_timestamp_id
        from brain.hc.database.task import TASK_KIND_ONETIME, TASK_STATUS_ACTIVE

        if action not in ACTION_STATES:
            raise ValueError("请选择统一维护工具列出的四种行动")
        # 维护行动一律立即执行：唤醒时间由系统固定为当前时间，不接受择时。
        next_occurrence = datetime.now().isoformat(timespec="seconds")
        details = dict(reason=reason, plan=plan, next_occurrence=next_occurrence,
                       notify_master=notify_master, verification=verification, event=event,
                       target=target, expectation=expectation)
        for key, value in {"title": title, **details}.items():
            if not isinstance(value, str):
                raise ValueError(f"{key} 必须为文字")
        required = ["reason", "plan"] + {
            "申请维护": ["notify_master"], "尝试自修复": ["verification"],
            "情绪宣泄": ["event", "target"], "发布关系改进任务": ["target", "expectation"],
        }[action]
        if not title.strip() or any(not details[key].strip() for key in required):
            raise ValueError("请填写标题及本类行动要求的原因、实施内容与专用信息")
        details = {key: value.strip() for key, value in details.items()}
        async with _action_lock:
            await self.recover_resets()
            state = get_feedback_state().snapshot()
            if action not in available_actions(state):
                raise ValueError("当前情感不符合该行动：效能感100申请维护、为0尝试自修复；归属感为0情绪宣泄、100发布关系改进任务")
            axis = ACTION_STATES[action][0]
            action_id = uuid4().hex
            conn = await get_memory_db()._ensure_connection()
            task_ids = []
            async with failure_reason_lock:
                reasons = await pending_failure_reasons() if action == "申请维护" else []
                try:
                    if action != "申请维护":
                        # 直接落库，不经 Quest：Quest 会改写上下文并按自己的规则查重/择时，
                        # 曾把行动标记洗掉导致核查失败、情感值不重置。维护待办必须立即到期。
                        task_id = generate_timestamp_id("Task")
                        now_text = datetime.now().isoformat()
                        await conn.execute(
                            """INSERT INTO tasks (task_id, title, context, kind, status,
                               next_occurrence, start_time, updated_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            (task_id, title.strip(), self._task_context(action, title.strip(), details),
                             TASK_KIND_ONETIME, TASK_STATUS_ACTIVE, next_occurrence, now_text, now_text))
                        task_ids.append(task_id)
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
                    "message": ("维护申请已发布，本批 failure_reason 已归入申请并清空待收集缓存。请立即向主人说明维护需求。"
                                if action == "申请维护" else "已登记立即执行的一次性待办，对应情感值已重置为50；请马上按计划去做。")}
