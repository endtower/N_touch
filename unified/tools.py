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


def _acts():
    """可认定的行为清单（来自主仓 act 表，排除系统内置类）。"""
    from .feelings import available_acts

    return available_acts()


@dataclass
class Tool:
    name: str
    authority: str
    summary: str
    params: Dict[str, str]
    handler: Callable[..., str]
    required: List[str] = field(default_factory=list)
    enum_field: str = ""
    enum_values: List[str] = field(default_factory=list)

    def schema(self) -> Dict[str, Any]:
        props = {k: {"type": "string", "description": v} for k, v in self.params.items()}
        if self.enum_field and self.enum_values:
            props.setdefault(self.enum_field, {"type": "string"})
            props[self.enum_field]["enum"] = list(self.enum_values)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": f"[{self.authority}] {self.summary}",
                "parameters": {
                    "type": "object",
                    "properties": props,
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


def build_toolbox(log=None, recall: Recall | None = None, dry: bool = False, state=None) -> ToolBox:
    box = ToolBox(log=log)
    mem = recall or Recall()

    def do_recall(query: str, k: str = "3") -> str:
        return mem.search(query, int(k))

    def do_recall_speech(query: str, k: str = "3") -> str:
        return mem.exp.recall_speech(query, int(k))

    def do_feel(act: str) -> str:
        if state is None:
            raise RuntimeError("feel 需要 state（情绪落在她自己的状态里）")
        from .feelings import feel

        return feel(state, log, act)

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
            "recall_speech",
            "capability",
            "只查“话术”经验：这种情况该怎么开口、这句话怎么说才不像机器。"
            "被说生硬、不知道第一句怎么接、想开玩笑/哄人/拒绝时先查它。",
            {"query": "当前处境或关键词", "k": "取几条"},
            do_recall_speech,
            required=["query"],
        )
    )
    box.register(
        Tool(
            "feel",
            "capability",
            "认定一件已经发生的事，从而改变你的情绪。\n"
            "注意：你不是直接拨 DA/NE 的数值，而是认定“发生了什么”——"
            "对方攻击你、夸奖你、晾着你、你自己推进顺利或搞砸，都在这张表里。\n"
            "调用后系统会自动返回你当前的情绪状态和描述。",
            {"act": "从可认定的行为表里选一个"},
            do_feel,
            required=["act"],
            enum_field="act",
            enum_values=_acts(),
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
