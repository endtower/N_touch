# -*- coding: utf-8 -*-
"""三个工具：查、命令、收尾。

  recall        capability  查经验 / 人 / 群
  cmd           capability  跑一条命令行（发文字、发图片、戳一戳）
  end_episode   decision    收尾并给效益分档

`cmd` 是"把对外动作收成一个入口"：她不再有一堆 send_xxx 工具，只有一条命令行。
发什么、发给谁、发图还是戳人，全在那一行里，一眼能看懂，也一眼能查账。

authority 不合并的理由没变：查和跑是 capability（留着不留结论），
end_episode 是 decision（一旦调用就是定性）。混进一个工具等于把权藏进参数。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List

from .history import Recall
from .qq_cli import run_command


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
            self.log.append("tool", {"tool": name, "authority": tool.authority, "args": args, "result": str(result)[:240]})
        return result


def build_toolbox(log=None, recall: Recall | None = None, dry: bool = False) -> ToolBox:
    box = ToolBox(log=log)
    mem = recall or Recall()

    def do_recall(query: str, k: str = "3") -> str:
        return mem.search(query, int(k))

    def do_cmd(command: str) -> str:
        return run_command(command, dry=dry)

    def end_episode(benefit: str, reason: str) -> str:
        return f"本轮收尾。效益={benefit}；依据={reason}。"

    box.register(
        Tool(
            "recall",
            "capability",
            "查你过去的记录：经验与锦囊（怎么应对）、某个人（印象与关系）、某个群（印象与自我定位）。",
            {"query": "人名、群名或当前困境", "k": "取几条"},
            do_recall,
            required=["query"],
        )
    )
    box.register(
        Tool(
            "cmd",
            "capability",
            "执行一条命令行，用来对外做事。只有三种：\n"
            "  qq text  <目标> <内容>      —— 发一句话\n"
            "  qq image <目标> <文件名>    —— 发一张图\n"
            "  qq poke  <目标> [QQ号]      —— 戳一戳（群戳要 QQ 号，私聊不用）\n"
            "目标写法：group_群号 / private_QQ号。一次只写一条命令。",
            {"command": "例如 qq text group_123 在呢"},
            do_cmd,
            required=["command"],
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
