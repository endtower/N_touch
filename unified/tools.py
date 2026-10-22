# -*- coding: utf-8 -*-
"""工具表（新内核）：常驻 7 + 冷库 3。

常驻
  lookup        通讯录：这个人/这个群是谁、什么印象、什么关系
  browse        「我这儿有什么」：话术经验 / 锦囊 / 见闻录 / 笔记 / 表情包
  feel          认定发生了什么 → 改情绪
  cmd           qq text / image / poke 一行命令
  sticker_view  把一张图读进上下文（多模态，不用 Vision Agent）
  fetch_tool    取冷库工具的要义
  end_episode   收尾 + 效益分档

冷库（先 fetch_tool 取要义，再执行）
  note_record   写记事本
  sticker_save  存表情包
  sticker_adjust 改表情包

两条硬约束：
1. 决策经验不启用——browse 只查 bequests 里 category='speech' 的。
2. “这人是谁”（lookup）和“这事该怎么办”（browse）不是一种查询，不合并。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List

from . import archive, media, notepad
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


def build_toolbox(log=None, dry: bool = False, state=None) -> ToolBox:
    box = ToolBox(log=log)

    def do_address_book(search_range: str = "全部", include_status: str = "") -> str:
        return archive.address_book(search_range, str(include_status).lower() in ("true", "1", "是"))

    def do_browse(query: str = "", kind: str = "all", k: str = "3") -> str:
        return archive.browse(query, kind, int(k))

    def do_feel(act: str) -> str:
        if state is None:
            raise RuntimeError("feel 需要 state")
        from .feelings import feel

        return feel(state, log, act)

    def do_cmd(command: str) -> str:
        return run_command(command, dry=dry)

    def do_sticker_view(ref: str) -> Any:
        return media.read_image(ref)

    def do_fetch(name: str) -> str:
        tool = box.tools.get(name)
        if tool is None:
            raise ValueError(f"没有叫 {name} 的工具")
        box.active.add(name)
        return f"已把 {name} 放进你的工具里，现在可以直接用了。\n{tool.spec_text()}"

    def end_episode(benefit: str, reason: str) -> str:
        return f"本轮收尾。效益={benefit}；依据={reason}。"

    def do_note_record(text: str, category: str = "general", title: str = "") -> str:
        return notepad.record(text, category, title)

    def do_sticker_save(src: str, title: str, mood: str = "", description: str = "") -> str:
        return media.save_sticker(src, title, mood, description)

    def do_sticker_adjust(ref: str, title: str = "", mood: str = "", description: str = "") -> str:
        return media.adjust_sticker(ref, title, mood, description)

    box.register(Tool("query_address_book", "capability",
                      "通讯录：你现在能跟谁联系。\n"
                      "  search_range 传「私聊」「群聊」「全部」→ 列出可联系的会话（带 session_id 与印象）；\n"
                      "  search_range 传具体的人名或群名 → 直接查你对他的印象。\n"
                      "发消息要用 cmd（qq text <session_id> <内容>），session_id 从这里拿。",
                      {"search_range": "私聊 / 群聊 / 全部，或具体人名群名",
                       "include_status": "填 true 会附带消息数与最后一条"},
                      do_address_book, required=["search_range"]))
    box.register(Tool("browse", "capability",
                      "查你手上都有什么：话术经验（这么说对不对）、锦囊（这种局面怎么办）、"
                      "见闻录（经历过的事）、笔记本、表情包。不带 kind 就全查一遍。",
                      {"query": "关键词，可空", "kind": "all / speech / kit / observation / note / sticker",
                       "k": "每类几条"},
                      do_browse))
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
    box.register(Tool("sticker_view", "capability",
                      "把库里的一张图读进你的上下文，你自己看。文件名从 browse(kind=sticker) 拿。",
                      {"ref": "文件名或标题关键词"}, do_sticker_view, required=["ref"]))
    box.register(Tool("fetch_tool", "capability",
                      "冷库工具不在你手上。想看某个冷库工具怎么用，先用它取要义，取完就能直接调。",
                      {"name": "工具名"}, do_fetch, required=["name"]))
    box.register(Tool("end_episode", "decision", "结束本轮。必须给出效益分档和依据。",
                      {"benefit": "+10|0|-5|-10|-20|-30", "reason": "依据"}, end_episode))

    box.register(Tool("note_record", "capability", "往记事本里记一笔。",
                      {"text": "要记的内容", "category": "分类，可空", "title": "短标题，可空"},
                      do_note_record, required=["text"], lazy=True))
    box.register(Tool("sticker_save", "capability", "把一张图存成表情包（写到 stickers 目录）。",
                      {"src": "源文件名", "title": "起个标题", "mood": "什么情绪下用", "description": "画面描述"},
                      do_sticker_save, required=["src", "title"], lazy=True))
    box.register(Tool("sticker_adjust", "capability", "改一张已有表情包的标题/情绪/描述。",
                      {"ref": "文件名", "title": "新标题", "mood": "新情绪", "description": "新描述"},
                      do_sticker_adjust, required=["ref"], lazy=True))
    return box
