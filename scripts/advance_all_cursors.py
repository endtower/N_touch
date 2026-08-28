#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性脚本：将 chat_history.db 中所有会话的游标全部移动至最新位置，确保全部消息已读。

说明：
- 基于游标制已读语义：未读 = seq > 游标 position；游标移至 max(seq) 即全部已读。
- 对所有会话，将核心消费者游标（conversation / pfca）推进到该会话消息最大 seq；
  会话中原已存在的其他消费者游标行同样一并推进，游标"全部"到最新。
- 全程使用 memory.message_repository（aiosqlite，全异步）；
  关键读写失败不做降级，直接崩溃阻断（沿用 async_handle_errors 的 raise_on 语义）。
- 幂等：重复执行结果不变（最多游标位置 = max seq）。

运行：python scripts/advance_all_cursors.py
"""

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from memory.message_repository import get_message_store

# 消息游标机制的核心消费者（对应 conversation_input_manager / pfca_input_manager 及 truck 未读检测）
CORE_CONSUMERS = ("conversation", "pfca")


async def _collect_consumer_ids(store, session_id: str) -> set[str]:
    """收集某会话上需推进的消费者：既有游标行 ∪ 核心消费者。"""
    conn = await store._ensure_connection()
    cur = await conn.execute(
        "SELECT consumer_id FROM cursors WHERE session_id=?",
        (session_id,),
    )
    rows = await cur.fetchall()
    return {r["consumer_id"] for r in rows} | set(CORE_CONSUMERS)


async def _advance_session(store, session_id: str) -> tuple[int, int, list[str]]:
    """将某会话全部游标推进到最新位置。

    Returns:
        (max_seq, 推进条数, 推进明细描述列表)
    """
    latest = await store.get_latest_session_messages(session_id, n=1)
    message_list = latest["message_list"]
    if not message_list:
        return -1, 0, []

    max_seq = message_list[-1]["_seq"]
    consumer_ids = await _collect_consumer_ids(store, session_id)

    details = []
    for consumer_id in sorted(consumer_ids):
        old_position = await store.get_cursor(session_id, consumer_id)
        await store.advance_cursor(session_id, consumer_id, max_seq)
        details.append(f"    {consumer_id}: {old_position} -> {max_seq}")

    return max_seq, len(consumer_ids), details


async def main() -> None:
    store = get_message_store()

    sessions = await store.list_sessions()
    if not sessions:
        print("[提示] 没有任何会话")
        await store.close()
        return

    changed = 0
    total_rows = 0
    for session in sessions:
        session_id = session["session_id"]
        max_seq, row_count, details = await _advance_session(store, session_id)
        if row_count == 0:
            continue
        print(f"[修改] {session_id}: 游标推进至 {max_seq}（{row_count} 个消费者）")
        for line in details:
            print(line)
        changed += 1
        total_rows += row_count

    await store.close()

    print("-" * 60)
    print(f"处理会话数: {len(sessions)}")
    print(f"发生推进的会话数: {changed}")
    print(f"推进的游标行数: {total_rows}")
    if changed == 0:
        print("没有任何会话需要推进游标。")


if __name__ == "__main__":
    asyncio.run(main())