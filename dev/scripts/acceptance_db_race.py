#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验收（GPT 审查-1）：空库 + 并发首访，只初始化一次，所有调用者等到就绪。

对应 docs/启动管理程序_架构审查与交接建议_2026-10-02.md 的验收场景：
"全新空数据目录，多协程同时首次访问，只进行一次完整初始化，所有调用者都等到就绪"。

运行：python tests/acceptance_db_race.py
"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memory.message_repository import MessageRepository  # noqa: E402
from utils.error_handling import StorageError  # noqa: E402


async def main() -> int:
    tmp = tempfile.mkdtemp(prefix="abyss_race_")
    repo = MessageRepository(storage_path=tmp)

    # 计数器：建表函数被真实执行的次数（放大竞态窗口的 50ms 延迟）
    counter = {"create": 0}
    original = repo._create_tables

    async def counted_create(conn):
        counter["create"] += 1
        await asyncio.sleep(0.05)
        await original(conn)

    repo._create_tables = counted_create

    # 10 个并发调用者同时首次访问（8 个查询 + 2 个就绪钩子）
    tasks = [repo.list_sessions() for _ in range(8)]
    tasks += [repo.ensure_ready(), repo.ensure_ready()]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    errors = [r for r in results if isinstance(r, BaseException)]
    if errors:
        print(f"✗ {len(errors)} 个并发调用者失败: {errors[:2]}")
        return 1
    if counter["create"] != 1:
        print(f"✗ 建表执行了 {counter['create']} 次（应恰好 1 次）——初始化竞态未修复")
        return 1

    print(f"✓ 10 个并发调用全部成功；建表恰好执行 1 次")

    # 事后库内确认
    import sqlite3
    conn = sqlite3.connect(str(Path(tmp) / "chat_history.db"))
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master").fetchall()]
    conn.close()
    if not {"sessions", "messages", "cursors"} <= set(tables):
        print(f"✗ 表结构缺失: {tables}")
        return 1
    print(f"✓ 库内表结构完整: {sorted(tables)}")

    await repo.close()

    # 关闭后再访问必须拒绝（而不是挂死或静默重建）。
    # 注意走未装饰的 _ensure_connection：list_sessions 的装饰器会把
    # StorageError 当"不可恢复"强制停止进程（项目崩溃阻断设计），不能在测试里触发。
    try:
        await repo._ensure_connection()
        print("✗ 关闭后访问未被拒绝")
        return 1
    except StorageError as e:
        print(f"✓ 关闭后访问被拒绝: StorageError({e})")

    print("\n验收通过：初始化竞态已修复")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
