# -*- coding: utf-8 -*-
"""工具表（新内核）。

12 个上限，分成两类：

常驻（一直在上下文里，她随时能用）
  recall          查经验/人/群
  recall_speech   只查话术
  feel            认定发生了什么 → 改情绪
  cmd             qq text / image / poke 一行命令
  sticker_list    看库里有什么
  sticker_view    把一张图读进上下文（多模态，不需要 Vision Agent）
  note_query      查记事本
  fetch_tool      拉取冷库工具的要义
  end_episode     收尾 + 效益分档

冷库（不在上下文里，先 fetch_tool 取要义再执行）
  note_record     写记事本
  sticker_save    把一张图存成表情包
  sticker_adjust  改表情包的标题/情绪/描述

为什么要冷库：写操作一周用不了几次，占着上下文只会稀释真正高频的那几个。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List

from . import media, notepad
from .history import Recall
from .qq_cli import run_command


def _acts():
    from .feelings import available_acts

    return available_acts()


@dataclass
class Tool:
    name: str
    authority: str
    summary: str
    params: Dict[str, str]
    handler: Callable[..., Any]
    required: List[str] = field(default_factory=list)
    enum_field: str = ""
    enum_values: List[str] = field(default_factory=list)
    lazy: bool = False

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

    def spec_text(self) -> str:
        args = "，".join(f"{k}（{v}）" for k, v in self.params.items())
        return f"{self.name}：{self.summary}\n  参数：{args}"


@dataclass
class ToolBox:
    tools: Dict[str, Tool] = field(default_factory=dict)
    log: Any = None
    active: set = field(default_factory=set)

    def register(self, tool: Tool) -> None:
        self.tools[tool.name] = tool
        if not tool.lazy:
            self.active.add(tool.name)

    def schemas(self) -> List[Dict[str, Any]]:
        """只发常驻 + 已被 fetch 的。"""
        return [t.schema() for name, t in self.tools.items() if name in self.active]

    def lazy_index(self) -> List[str]:
        return [t.name for t in self.tools.values() if t.lazy and t.name not in self.active]

    def call(self, name: str, args: Dict[str, Any]) -> Any:
        tool = self.tools.get(name)
        if tool is None:
            raise ValueError(f"没有这个工具: {name}（冷库里有：{'、'.join(self.lazy_index()) or '无'}）")
        if tool.lazy and name not in self.active:
            raise ValueError(f"「{name}」在冷库里，先用 fetch_tool 取它的要义，再执行")
        result = tool.handler(**{k: v for k, v in args.items() if k in tool.params})
        if self.log is not None:
            brief = result if isinstance(result, str) else "(图片)" if isinstance(result, dict) and result.get("__image__") else str(result)
            self.log.append("tool", {"tool": name, "authority": tool.authority, "args": args, "result": str(brief)[:240]})
        return result


def build_toolbox(log=None, recall: Recall | None = None, dry: bool = False, state=None) -> ToolBox:
    box = ToolBox(log=log)
    mem = recall or Recall()

    # ---------- 常驻 ----------

    def do_recall(query: str, k: str = "3") -> str:
        return mem.search(query, int(k))

    def do_recall_speech(query: str, k: str = "3") -> str:
        return mem.exp.recall_speech(query, int(k))

    def do_feel(act: str) -> str:
        if state is None:
            raise RuntimeError("feel 需要 state")
        from .feelings import feel

        return feel(state, log, act)

    def do_cmd(command: str) -> str:
        return run_command(command, dry=dry)

    def do_sticker_list(kind: str = "stickers", query: str = "", limit: str = "20") -> str:
        return media.list_items(kind or "stickers", query, int(limit))

    def do_sticker_view(ref: str) -> Any:
        return media.read_image(ref)

    def do_note_query(keyword: str = "", limit: str = "5") -> str:
        return notepad.query(keyword, int(limit))

    def do_fetch(name: str) -> str:
        tool = box.tools.get(name)
        if tool is None:
            raise ValueError(f"没有叫 {name} 的工具")
        box.active.add(name)
        return f"已把 {name} 放进你的工具里，现在可以直接用了。\n{tool.spec_text()}"

    def end_episode(benefit: str, reason: str) -> str:
        return f"本轮收尾。效益={benefit}；依据={reason}。"

    # ---------- 冷库 ----------

    def do_note_record(text: str, category: str = "general", title: str = "") -> str:
        return notepad.record(text, category, title)

    def do_sticker_save(src: str, title: str, mood: str = "", description: str = "") -> str:
        return media.save_sticker(src, title, mood, description)

    def do_sticker_adjust(ref: str, title: str = "", mood: str = "", description: str = "") -> str:
        return media.adjust_sticker(ref, title, mood, description)

    box.register(Tool("recall", "capability", "查你过去的记录：经验与锦囊、某个人、某个群。",
                      {"query": "人名、群名或当前困境", "k": "取几条"}, do_recall, required=["query"]))
    box.register(Tool("recall_speech", "capability", "只查“话术”经验：这种话该怎么说、怎么开口才不像机器。",
                      {"query": "当前处境或关键词", "k": "取几条"}, do_recall_speech, required=["query"]))
    box.register(Tool("feel", "capability",
                      "认定一件已经发生的事，从而改变你的情绪。你不是直接拨数值，而是认定“发生了什么”。"
                      "调用后系统会自动返回你当前的情绪状态。",
                      {"act": "从可认定的行为表里选一个"}, do_feel, required=["act"],
                      enum_field="act", enum_values=_acts()))
    box.register(Tool("cmd", "capability",
                      "执行一条命令行，用来对外做事：\n"
                      "  qq text  <目标> <内容>    发一句话\n"
                      "  qq image <目标> <文件名>  发一张图\n"
                      "  qq poke  <目标> [QQ号]    戳一戳（群戳要 QQ 号）\n"
                      "目标写法：group_群号 / private_QQ号。一次只写一条。",
                      {"command": "例如 qq text group_123 在呢"}, do_cmd, required=["command"]))
    box.register(Tool("sticker_list", "capability", "看表情包/图片库里有什么（可按关键词筛）。",
                      {"kind": "stickers 或 images", "query": "关键词，可空", "limit": "最多几条"},
                      do_sticker_list))
    box.register(Tool("sticker_view", "capability",
                      "把库里的一张图读进你的上下文，你自己看。文件名从 sticker_list 拿。",
                      {"ref": "文件名或标题关键词"}, do_sticker_view, required=["ref"]))
    box.register(Tool("note_query", "capability", "查记事本里记过什么。",
                      {"keyword": "关键词，可空", "limit": "几条"}, do_note_query))
    box.register(Tool("fetch_tool", "capability",
                      "冷库工具不在你手上。想看某个冷库工具怎么用，先用它取要义，取完就能直接调。",
                      {"name": "工具名"}, do_fetch, required=["name"]))
    box.register(Tool("end_episode", "decision", "结束本轮。必须给出效益分档和依据。",
                      {"benefit": "+10|0|-5|-10|-20|-30", "reason": "依据"}, end_episode))

    box.register(Tool("note_record", "capability", "往记事本里记一笔。",
                      {"text": "要记的内容", "category": "分类，可空", "title": "短标题，可空"},
                      do_note_record, required=["text"], lazy=True))
    box.register(Tool("sticker_save", "capability", "把一张图存成表情包（会写到 stickers 目录）。",
                      {"src": "源文件名", "title": "起个标题", "mood": "什么情绪下用", "description": "画面描述"},
                      do_sticker_save, required=["src", "title"], lazy=True))
    box.register(Tool("sticker_adjust", "capability", "改一张已有表情包的标题/情绪/描述。",
                      {"ref": "文件名", "title": "新标题", "mood": "新情绪", "description": "新描述"},
                      do_sticker_adjust, required=["ref"], lazy=True))
    return box
