# -*- coding: utf-8 -*-
"""
Amy 模块状态契约（stable public API）

供预设测试环境使用：把 amy 的 thought 持久化状态导出/还原为可读 JSON
（崩溃/覆盖机制已移除，system_state 载荷随之取消）。
- 载体：JSON（可肉眼编辑、可版本化），见 docs/archive/preset_test_env_design.md
- 归属：本契约归 Amy 模块暴露；tests 只做隔离运行时的组装与应用。
- 全异步；任何获取失败直接抛异常阻断，不用 try-ex 兜底继续。
- 注：`_consumers` 为实例级、EmotionalInterface 重建即空，不纳入快照。
"""

import json
from pathlib import Path
from typing import Dict, List, Any

from brain.amy.emotional_interface import get_emotional_interface


def _read_json(path: Path) -> Dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class AmyState:
    """Amy 模块状态契约（stable public API，JSON 载体）。

    属性：
        thoughts:     thought_cache.json 内容（thoughts + timestamp）
    """

    def __init__(self, thoughts: Dict[str, Any]) -> None:
        self.thoughts = thoughts

    @classmethod
    async def capture(cls) -> "AmyState":
        """从当前实例（单例指向的 persistence 目录）导出状态。"""
        ei = await get_emotional_interface()
        thoughts = _read_json(ei.thought_cache.persistence_file)
        return cls(thoughts=thoughts)

    @classmethod
    async def capture_from(cls, persistence_dir) -> "AmyState":
        """从**显式指定**的真实 persistence 目录导出状态（与当前单例指向无关）。

        供"正式 amy 环境镜像"抓取使用；目录不存在则对应状态为空 dict。
        """
        persistence_dir = Path(persistence_dir)
        return cls(
            thoughts=_read_json(persistence_dir / "thought_cache.json"),
        )

    async def restore(self, persistence_dir: Path) -> None:
        """在隔离 persistence 目录写回 thought_cache.json / system_state.json。

        随后复用 get_emotional_interface(persistence_dir) 把单例指向隔离目录
        （其 bootstrap 对已存在的系统 act 自动跳过）。
        须在 isolated_storage 作用域内调用。
        """
        persistence_dir = Path(persistence_dir)
        _write_json(persistence_dir / "thought_cache.json", self.thoughts)
        await get_emotional_interface(persistence_dir)

    # ---- 情景文档（JSON 文件）存/取 ----

    def to_json_file(self, path: Path) -> None:
        """导出为情景文档（可手动编辑）。"""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "module": "amy",
            "state": {
                "thoughts": self.thoughts,
            },
        }
        _write_json(path, payload)

    @classmethod
    async def from_json_file(cls, path: Path) -> "AmyState":
        """读入预设情景文档，复用 restore 复现。"""
        path = Path(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("module") != "amy":
            raise ValueError(f"情景文件 {path.name} 不属于 amy 模块")
        state = data["state"]
        return cls(
            thoughts=state.get("thoughts", {}),
        )