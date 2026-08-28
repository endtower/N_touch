#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""验证重构后的 memory 模块：SQLite 存储 + 游标机制。

运行：python scripts/verify_memory.py
"""

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from memory.message_repository import get_message_store


async def main() -> None:
    store = get_message_store()

    # 1. 会话列表（从 SQLite 读）
    sessions = await store.list_sessions()
    print(f"[1] 会话列表: {len(sessions)} 个")
    for s in sessions[:3]:
        print(f"    {s['session_id']}: {s['message_count']} 条消息")

    # 2. 读取一个会话的消息
    if sessions:
        session_id = sessions[0]["session_id"]
        result = await store.get_latest_session_messages(session_id, n=3)
        print(f"[2] 读取 {session_id} 最近 3 条:")
        for m in result["message_list"]:
            print(f"    [{m['user_name']}] {m['content_text'][:30]}")

    # 3. 游标机制
    test_session = sessions[0]["session_id"] if sessions else "private_test"
    consumer = "conversation"
    cursor = await store.get_cursor(test_session, consumer)
    print(f"[3] 游标初始位置: {cursor}")

    # 获取未读（游标之后的消息）
    unread = await store.get_unread_for(test_session, consumer, limit=5)
    print(f"[4] 未读消息（limit=5）: {len(unread)} 条")

    # 推进游标到这批消息的最大 seq
    if unread:
        max_seq = max(m["_seq"] for m in unread)
        await store.advance_cursor(test_session, consumer, max_seq)
        new_cursor = await store.get_cursor(test_session, consumer)
        print(f"[5] 推进游标到 {max_seq}，当前游标: {new_cursor}")

        # 再次获取未读，应该不重复
        unread2 = await store.get_unread_for(test_session, consumer, limit=5)
        print(f"[6] 再次获取未读: {len(unread2)} 条（应不包含已读的）")

    # 4. 会话状态（游标制：read 计数已废弃，仅元数据 + 总数）
    if sessions:
        session_id = sessions[0]["session_id"]
        status = await store.get_session_status(session_id)
        print(f"[7] 会话状态: 总数 {status['total_count']}, 名称 {status['session_info'].get('session_name', '')}")

    await store.close()
    print("\n验证完成。")


if __name__ == "__main__":
    asyncio.run(main())
