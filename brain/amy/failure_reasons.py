"""结束评价的 failure_reason 待维护缓存；申请发布时整批移入该申请。"""
import asyncio
import time
from uuid import uuid4

from brain.amy.constants import EvaluationAct, FinalEvaluationAct, TaskResultAct
from brain.hc.database.core import get_memory_db

failure_reason_lock = asyncio.Lock()


def is_failure_evaluation(act):
    return EvaluationAct.is_failure(act) or FinalEvaluationAct.is_failure(act) or TaskResultAct.is_failure(act)


def validate_failure_reason(act, failure_reason):
    if failure_reason is not None and not isinstance(failure_reason, str):
        raise ValueError("failure_reason 必须为文字")
    if is_failure_evaluation(act) and not (failure_reason or "").strip():
        raise ValueError("选择负面结束评价时须填写 failure_reason，说明本次表现差的具体原因")


async def cache_failure_reason(act, failure_reason, *, source, title, session_id=""):
    validate_failure_reason(act, failure_reason)
    if not is_failure_evaluation(act):
        return
    conn = await get_memory_db()._ensure_connection()
    async with failure_reason_lock:
        await conn.execute("INSERT INTO failure_reason_cache VALUES (?,?,?,?,?,?,?)",
                           (uuid4().hex, source, session_id, title, act, failure_reason.strip(), time.time()))
        await conn.commit()


async def pending_failure_reasons():
    conn = await get_memory_db()._ensure_connection()
    return [dict(row) for row in await (await conn.execute("SELECT * FROM failure_reason_cache ORDER BY created_at,reason_id")).fetchall()]
