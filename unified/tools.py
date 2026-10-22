# -*- coding: utf-8 -*-
"""统一工具注册表：两个工具。

  recall        —— 查。经验、锦囊、人、群，一个入口。
  end_episode   —— 定。这轮到底有没有用。

为什么只剩两个（从 6 个精简下来的）：

- `look_up` / `look_up_group` 合成 `recall`：人不会先判断"我要查的是做法还是某个人"。
  分区是数据库的事，不是她的事。
- `wait` 删掉：它就是 Conversation 时代的遗物。那时候有个"说话的人"在等对方回复；
  现在消息每拍由 Feed 推过来，没有什么可等的，留着只会让她空转。
- `note` 删掉：它写进一个没人读的列表，等于有出口没去处。要么接真持久化，要么不留。

为什么不合成一个：`recall` 是 capability（查了不留结论），`end_episode` 是 decision
（一旦调用就是定性）。这两类的 authority 不同，混进一个工具就等于把权限藏进参数里，
和我们保留的那唯一一条边界冲突。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List

from .history import Recall


@dataclass
class Tool:
    name: str
    authority: str
    summary: str
    params: Dict[str, str]
    handler: Callable[..., str]
    required: List[str] = field(default_factory=list)

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": f"[{self.authority}] {self.summary}",
                "parameters": {
                    "type": "object",
                    "properties": {k: {"type": "string", "description": v} for k, v in self.params.items()},
                    "required": self.required or list(self.params),
                },
            },
        }


@dataclass
class ToolBox:
    tools: Dict[str, Tool] = field(default_factory=dict)
    log: Any = None

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


def build_toolbox(log=None, recall: Recall | None = None) -> ToolBox:
    box = ToolBox(log=log)
    mem = recall or Recall()

    def do_recall(query: str, k: str = "3") -> str:
        return mem.search(query, int(k))

    def end_episode(benefit: str, reason: str) -> str:
        return f"本轮收尾。效益={benefit}；依据={reason}。"

    box.register(
        Tool(
            "recall",
            "capability",
            "查你过去的记录：经验与锦囊（该怎么应对）、某个人（印象与关系）、某个群（印象与自我定位）。"
            "不知道怎么做、被说生硬、想开口找不到由头、不确定对方是谁时用它。",
            {"query": "人名、群名或当前困境", "k": "取几条"},
            do_recall,
            required=["query"],
        )
    )
    box.register(
        Tool(
            "end_episode",
            "decision",
            "结束本轮。必须给出效益分档和依据，给不出依据就别结束。",
            {"benefit": "+10|0|-5|-10|-20|-30", "reason": "依据"},
            end_episode,
        )
    )
    return box
