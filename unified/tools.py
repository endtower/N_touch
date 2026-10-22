# -*- coding: utf-8 -*-
"""统一工具注册表（PFC-only 版）。

关键改动：**没有 conversation_agent 这个工具了。**

原来它是"派一个说话的人去一段会话"，现在是：
  - 该会话的最新消息由 loop 每拍自动拉给你（Feed）
  - 缺的是"怎么应对这类局面"，用 recall_experience 自己取（Experience）

authority:
  capability  —— 看、查、记、等。随便用，不留结论。
  decision    —— 给人下结论、结束本轮、结算效益。必须产出结算记录。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List

from .history import Experience, People, as_json


@dataclass
class Tool:
    name: str
    authority: str
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
    tools: Dict[str, Tool] = field(default_factory=dict)
    log: Any = None
    notes: List[str] = field(default_factory=list)

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
            self.log.append("tool", {"tool": name, "authority": tool.authority, "args": args, "result": result[:240]})
        return result


def build_toolbox(log=None, experience: Experience | None = None, people: People | None = None) -> ToolBox:
    box = ToolBox(log=log)
    exp = experience or Experience()
    ppl = people or People()

    def recall_experience(query: str, k: str = "3") -> str:
        return exp.recall(query, int(k))

    def look_up(person: str) -> str:
        hit = ppl.get(person)
        return as_json(hit) if hit else f"通讯录里没有「{person}」。"

    def look_up_group(name: str) -> str:
        hit = ppl.get_group(name)
        return as_json(hit) if hit else f"没有关于这个群的记录：{name}"

    def note(text: str) -> str:
        box.notes.append(text)
        return "已记下。"

    def wait(seconds: str = "20") -> str:
        return f"等待 {seconds} 秒。"

    def end_episode(benefit: str, reason: str) -> str:
        return f"本轮收尾。效益={benefit}；依据={reason}。"

    box.register(
        Tool(
            "recall_experience",
            "capability",
            "取和你当前处境相关的经验与锦囊。不知道怎么做、被说生硬、想开口却找不到话时用它。",
            {"query": "当前困境或关键词", "k": "取几条"},
            recall_experience,
        )
    )
    box.register(Tool("look_up", "capability", "查某个人在你这里的印象与关系。", {"person": "人名"}, look_up))
    box.register(Tool("look_up_group", "capability", "查这个群给你的印象和你在群里的自我定位。", {"name": "群名"}, look_up_group))
    box.register(Tool("note", "capability", "把一件事记下来，以后能取到。", {"text": "要记的内容"}, note))
    box.register(Tool("wait", "capability", "等一会儿再看，别急着补话。", {"seconds": "秒数"}, wait))
    box.register(
        Tool(
            "end_episode",
            "decision",
            "结束本轮。必须给出效益分档和依据，否则不算完成。",
            {"benefit": "+10|0|-5|-10|-20|-30", "reason": "依据"},
            end_episode,
        )
    )
    return box
