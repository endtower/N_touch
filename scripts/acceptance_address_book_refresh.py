# -*- coding: utf-8 -*-
"""通讯录刷新验收：新会话与刚收到的消息必须立即可见（不重启进程）。

使用临时 SQLite 与临时数据根，不接触真实数据库或聊天数据。
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_sandbox = tempfile.TemporaryDirectory(prefix="abyss-addressbook-")
os.environ["ABYSS_DATA_DIR"] = _sandbox.name
_personas = Path(_sandbox.name) / "brain" / "base" / "persona"
_personas.mkdir(parents=True)
for _name in ("common", "normal"):
    (_personas / f"{_name}.json").write_text(json.dumps({
        "persona_name": _name, "scene": "隔离验收", "kernel_prompt": "占位",
    }), encoding="utf-8")

from brain.hc.database.core import switch_memory_db  # noqa: E402
from memory.message_repository import get_message_store  # noqa: E402
from memory.address_book import AddressBook  # noqa: E402


def message(mid: str, user: str, text: str, ts: float) -> dict:
    return {"message_id": mid, "user_id": user, "user_name": user,
            "timestamp": ts, "content_text": text, "content": []}


async def store(session_id: str, name: str, messages: list) -> None:
    await get_message_store().store_message({
        "session_info": {"session_id": session_id, "session_name": name,
                         "remark": "", "last_update_time": messages[-1]["timestamp"]},
        "message_list": messages,
    })


async def main() -> None:
    db_path = Path(_sandbox.name) / "memory" / "memory.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    await switch_memory_db(str(db_path))

    book = AddressBook()

    now = time.time()
    await store("private_1001", "老王", [message("m1", "老王", "在吗", now - 60)])
    first = await book.get_private_contacts(include_status=True)
    assert "老王" in first, first
    print("[1] 首次读取包含老王 OK")

    # 新会话：不重启进程，直接再读
    await store("private_1002", "小李", [message("m2", "小李", "你好", now)])
    second = await book.get_private_contacts(include_status=True)
    assert "小李" in second, second
    assert "老王" in second, second
    print("[2] 新会话立即出现 OK")

    # 已有会话收到新回复：最后一条消息必须同步
    await store("private_1001", "老王", [message("m3", "老王", "刚回来的回复", now + 1)])
    third = await book.get_private_contacts(include_status=True)
    assert "刚回来的回复" in third, third
    print("[3] 新回复立即反映到最后一条 OK")

    # 重复读取不得重复追加（原实现每轮 append）
    again = await book.get_private_contacts(include_status=True)
    assert again.count("联系方式：private_1001") == 1, again
    assert again.count("联系方式：private_1002") == 1, again
    print("[4] 重复读取不重复追加 OK")

    # 群聊与全部视图
    await store("group_2001", "测试群", [message("g1", "群友", "冒泡", now)])
    groups = await book.get_group_contacts()
    assert "测试群" in groups, groups
    everything = await book.get_all_contacts()
    assert "老王" in everything and "测试群" in everything, everything
    print("[5] 群聊与全部视图 OK")

    # 并发读取：不得因锁竞争出错，且结果一致
    results = await asyncio.gather(*(book.get_private_contacts() for _ in range(8)))
    assert all("小李" in r and "老王" in r for r in results), results
    print("[6] 并发读取一致 OK")

    # 印象缓存失效后重查（不崩、不返回旧缓存）
    await book.refresh_impression("private_1001")
    assert "老王" in await book.get_private_contacts()
    print("[7] 印象缓存失效后重查 OK")

    print("\n通讯录刷新验收全部通过")


if __name__ == "__main__":
    asyncio.run(main())
