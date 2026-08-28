#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
数据迁移脚本：把旧版 chat_history/*.json 迁移到 SQLite。

运行：python scripts/migrate_chat_history.py

说明：
- 读取 memory/chat_history/*.json（旧版 JSON 存储）。
- 通过 MessageRepository.store_message 导入 SQLite（chat_history.db）。
- 迁移完成后，JSON 文件保留（不删除），以便回退。
"""

import asyncio
import json
import sys
from pathlib import Path

# 添加项目根目录到 sys.path（脚本在 scripts/ 下运行）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from memory.message_repository import get_message_store


async def migrate() -> None:
    chat_history_dir = Path(__file__).resolve().parent.parent / "memory" / "chat_history"
    store = get_message_store()

    json_files = sorted(chat_history_dir.glob("*.json"))
    if not json_files:
        print("没有找到 JSON 文件，无需迁移")
        return

    total_sessions = 0
    total_messages = 0

    for file_path in json_files:
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                session_data = json.load(f)

            session_info = session_data.get("session_info", {})
            message_list = session_data.get("message_list", [])
            session_id = session_info.get("session_id", file_path.stem)

            if not message_list:
                print(f"跳过空会话: {session_id}")
                continue

            # 通过 repository 导入
            result = await store.store_message(session_data)
            total_sessions += 1
            total_messages += len(message_list)
            print(f"迁移会话 {session_id}: {len(message_list)} 条消息")
        except Exception as e:
            print(f"迁移失败 {file_path.name}: {e}")

    print(f"\n迁移完成: {total_sessions} 个会话, {total_messages} 条消息")
    print(f"SQLite 数据库: {chat_history_dir / 'chat_history.db'}")

    # 关闭数据库连接，避免进程挂起
    await store.close()


if __name__ == "__main__":
    asyncio.run(migrate())
