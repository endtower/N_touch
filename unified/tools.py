# -*- coding: utf-8 -*-
"""统一工具注册表：全部工具对一个 agent 可见，但按 authority 分层。

authority:
  capability  —— 看、查、记、等。随便用，不留结论。
  decision    —— 给人下结论、结束本轮、结算效益。必须产出结算记录。

分工取消之后，"我能做"和"我有权定性"这条线是唯一剩下的边界。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class Tool:
    name: str
    authority: str  # capability | decision
    summary: str
    params: Dict[str, str]
    handler: Callable[..., str]

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": f"[{self.authority}] {self.summary}",
                "parameters": {
                    "type": "object",
                    "properties": {k: {"type": "string", "description": v} for k, v in self.params.items()},
                },
            },
        }


@dataclass
class ToolBox:
    """一个 agent 的工具集——不再有"谁的工具"，只有"谁的权限"。"""

    tools: Dict[str, Tool] = field(default_factory=dict)
    log: Any = None
    memory: List[Dict[str, str]] = field(default_factory=list)
    people: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def register(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def schemas(self) -> List[Dict[str, Any]]:
        return [t.schema() for t in self.tools.values()]

    def call(self, name: str, args: Dict[str, Any]) -> str:
        tool = self.tools.get(name)
        if tool is None:
            raise ValueError(f"没有这个工具: {name}")
        result = tool.handler(**{k: v for k, v in args.items() if k in tool.params})
        if self.log is not None:
            self.log.append(
                "tool",
                {"tool": name, "authority": tool.authority, "args": args, "result": result[:200]},
            )
        return result


# ---------------- 具体工具 ----------------


def build_toolbox(log=None) -> ToolBox:
    box = ToolBox(log=log)

    def recall(query: str, limit: str = "5") -> str:
        hits = [m for m in box.memory if query and query in m["text"]][: int(limit)]
        if not hits:
            return f"没有找到和「{query}」相关的记录。"
        return "\n".join(f"- {h['speaker']}: {h['text']}" for h in hits)

    def note(text: str) -> str:
        box.memory.append({"speaker": "note", "text": text})
        return "已记下。"

    def look_up(person: str) -> str:
        p = box.people.get(person)
        return json.dumps(p, ensure_ascii=False) if p else f"通讯录里没有「{person}」。"

    def wait(seconds: str = "20") -> str:
        return f"等待 {seconds} 秒后没有新消息。"

    def end_episode(benefit: str, reason: str) -> str:
        # decision 型：必须留下结算记录，否则这一步不算完成
        return f"本轮已收尾。效益={benefit}；依据={reason}。"

    box.register(Tool("recall", "capability", "按关键词检索历史消息与笔记。", {"query": "关键词", "limit": "条数"}, recall))
    box.register(Tool("note", "capability", "把一件事写进笔记，供以后检索。", {"text": "要记的内容"}, note))
    box.register(Tool("look_up", "capability", "查通讯录里某个人的印象与关系。", {"person": "人名"}, look_up))
    box.register(Tool("wait", "capability", "在当前会话等一会儿，看对方有没有补充。", {"seconds": "秒数"}, wait))
    box.register(
        Tool(
            "end_episode",
            "decision",
            "结束本轮。必须同时给出效益分档和依据，否则不算完成。",
            {"benefit": "+10|0|-5|-10|-20|-30", "reason": "依据"},
            end_episode,
        )
    )
    return box
