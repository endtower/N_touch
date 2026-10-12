# -*- coding: utf-8 -*-
"""见闻录 / 任务相关笔记展示结构示例（临时目录，不污染真实笔记）。

运行：python scripts/demo_slot_notes_view.py
"""

import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_sandbox = tempfile.TemporaryDirectory(prefix="shortlight_demo_")
os.environ["ABYSS_DATA_DIR"] = _sandbox.name

import brain.hc.shortlight.notepad as nmod
from brain.hc.shortlight.notepad import ShortlightNotepad, SLOT_EVENT, SLOT_TASK
from brain.base.common_ways.tool_hc import ToolHC


async def main() -> None:
    # 用临时目录实例替换单例，避免污染真实 notes
    tmp = Path(_sandbox.name)
    np = ShortlightNotepad()
    np._notes_dir = tmp
    np._slots_dir = tmp / "slots"
    np._slots_dir.mkdir(parents=True, exist_ok=True)
    nmod._instance = np

    # 模拟一条事件链，记录任务与自我修正素材。
    np.begin_chain("chain_demo", trigger_text="主人让我记住他的偏好")
    await np.append_note("任务A需先确认时间", "pfca", "任务进度相关")
    await np.append_note("主人演示了更简洁的写法", "conversation", "自我修正")
    # 真正结束：事件经过随总结保存，任务素材仍双写任务槽位。
    await np.finalize_chain("记住主人的偏好", summary="主人不喜欢香菜")
    # 反思独立记录见闻，不要求活跃事件链。
    await np.record_observation("本次讨论中，主人主动补充饮食偏好；可能希望后续建议考虑个人习惯。")

    print("=" * 60)
    print("list_notes(scope='见闻录'):")
    print(await ToolHC.list_notes("见闻录"))

    print("=" * 60)
    print("list_notes(scope='任务相关笔记'):")
    print(await ToolHC.list_notes("任务相关笔记"))

    print("=" * 60)
    print("list_notes()  # 默认普通笔记")
    print(await ToolHC.list_notes())

    print("=" * 60)
    print(f"（示例数据写在临时目录：{tmp}）")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        logging.shutdown()
        _sandbox.cleanup()
