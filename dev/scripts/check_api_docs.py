#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
check_api_docs — API 文档漂移校验

从代码中抽取当前真实的 API 面，核对每一条是否已写入 docs/api/ 下对应文档：

  1. HTTP 路由     adapt/web/*.py 中 router.add_get/post/patch/delete("...") 的路径
  2. WS 协议       adapt/web/channel.py 的客户端 action（kind == "..."）与服务端
                   消息类型（"type": "..."）→ docs/api/web_api.md
  3. 单例访问器    四个源码根（adapt/ memory/ brain/ core/ utils/）下模块顶层
                   get_* / switch_* / reset_* 函数 → 按模块映射的文档
  4. LLM 工具名    4 个工具注册表中的 "name": "..." → 对应文档

规则（与 AGENTS.md「文档同步义务」一致）：
  - 有条目未出现在文档 → 打印漂移清单，退出码 1
  - 本脚本不做参数级校验（签名/语义仍需人工维护），只保证「接口存在 ⟹ 文档提及」
  - 新增模块时在 MODULE_DOC_MAP 里登记映射；故意不暴露的接口在文档中说明后即可通过

用法：
  python scripts/check_api_docs.py
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_API = REPO_ROOT / "docs" / "api"

# 源码路径前缀（相对仓库根，posix 风格）→ 必须提及该接口的文档
MODULE_DOC_MAP = [
    ("adapt/web/",        "web_api.md"),
    ("adapt/onebot/",     "onebot_api.md"),
    ("adapt/",            "onebot_api.md"),   # manager / router / protocol 等顶层文件
    ("memory/",           "message_store.md"),
    ("brain/amy/",        "amy.md"),
    ("brain/att/",        "attention.md"),
    ("brain/pfc/",        "pfca.md"),
    ("brain/chat/conversation/", "conversation.md"),
    ("brain/chat/social_chat/",  "social_chat.md"),
    ("brain/chat/",       "conversation.md"),  # 共享件（chat_history_manager 等）记入 conversation.md
    ("brain/hc/",         "hc.md"),
    ("brain/base/",       "infrastructure.md"),
    ("core/",             "infrastructure.md"),
    ("utils/",            "infrastructure.md"),
]

# LLM 工具注册表文件 → 文档
TOOL_FILES = [
    ("brain/pfc/pfca/pfca_tools.py",                         "pfca.md"),
    ("brain/att/proactive/proactive_tools.py",               "attention.md"),
    ("brain/chat/conversation/conversation_tool_manager.py", "conversation.md"),
    ("brain/chat/social_chat/social_chat_tool_manager.py",   "social_chat.md"),
]

SOURCE_ROOTS = ["adapt", "memory", "brain", "core", "utils"]
ACCESSOR_PREFIXES = ("get_", "switch_", "reset_")

DRIFT = []  # (类别, 条目, 代码位置, 应出现的文档)


def doc_text(name: str) -> str:
    return (DOCS_API / name).read_text(encoding="utf-8")


def find_doc(rel_path: str) -> str:
    p = rel_path.replace("\\", "/")
    for prefix, doc in MODULE_DOC_MAP:
        if p.startswith(prefix):
            return doc
    return ""


def rel(p: Path) -> str:
    return str(p.relative_to(REPO_ROOT)).replace("\\", "/")


def py_files() -> list[Path]:
    files = []
    for root in SOURCE_ROOTS:
        files.extend((REPO_ROOT / root).rglob("*.py"))
    return [f for f in files if "__pycache__" not in f.parts]


# ---------------------------------------------------------------- 路由抽取

ROUTE_RE = re.compile(
    r"router\.add_(get|post|patch|delete|put)\(\s*['\"]([^'\"]+)['\"]", re.M
)


def extract_routes() -> list[tuple[str, str, str]]:
    """返回 [(method path, 代码位置, 文档)]"""
    out = []
    for f in (REPO_ROOT / "adapt" / "web").glob("*.py"):
        for m in ROUTE_RE.finditer(f.read_text(encoding="utf-8")):
            method, path = m.group(1).upper(), m.group(2)
            if path.startswith("{"):  # 变量路径（如 self.config.path）静态抽不到，人工维护
                continue
            path = path.replace(":.*}", "}")  # aiohttp 正则尾缀归一化，便于文档子串匹配
            out.append((f"{method} {path}", f"{rel(f)}", "web_api.md"))
    return out


# ---------------------------------------------------------------- WS 协议抽取

ACTION_RE = re.compile(r'kind == "([^"]+)"')
MSGTYPE_RE = re.compile(r'"type":\s*"([^"]+)"')


def extract_ws() -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    ch = REPO_ROOT / "adapt" / "web" / "channel.py"
    text = ch.read_text(encoding="utf-8")
    actions = [(a, f"{rel(ch)}", "web_api.md") for a in sorted(set(ACTION_RE.findall(text)))]
    pushes = [(t, f"{rel(ch)}", "web_api.md") for t in sorted(set(MSGTYPE_RE.findall(text)))]
    return actions, pushes


# ---------------------------------------------------------------- 访问器抽取

def extract_accessors() -> list[tuple[str, str, str]]:
    out = []
    for f in py_files():
        doc = find_doc(rel(f))
        if not doc:
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in tree.body:  # 仅模块顶层
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith(ACCESSOR_PREFIXES) and not node.name.startswith("__"):
                    out.append((f"{node.name}()", f"{rel(f)}:{node.lineno}", doc))
    return out


# ---------------------------------------------------------------- 工具名抽取

TOOLNAME_RE = re.compile(r'"name":\s*"([^"]+)"')


def extract_tools() -> list[tuple[str, str, str]]:
    out = []
    for relf, doc in TOOL_FILES:
        f = REPO_ROOT / relf
        for name in sorted(set(TOOLNAME_RE.findall(f.read_text(encoding="utf-8")))):
            out.append((f"tool:{name}", relf, doc))
    return out


# ---------------------------------------------------------------- 主流程

def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    cache: dict[str, str] = {}

    def check(kind: str, item: str, where: str, doc: str, needle: str | None = None) -> None:
        """needle 缺省用 item 原文；文档中只需出现 needle 子串即算覆盖。"""
        text = cache.get(doc)
        if text is None:
            path = DOCS_API / doc
            if not path.exists():
                DRIFT.append((kind, item, where, f"{doc}（文档不存在！）"))
                return
            text = doc_text(doc)
            cache[doc] = text
        if (needle or item) not in text:
            DRIFT.append((kind, item, where, doc))

    for item, where, doc in extract_routes():
        method, path = item.split(" ", 1)
        check("HTTP 路由", item, where, doc, needle=path)  # 路径子串即覆盖（方法级人工保证）

    actions, pushes = extract_ws()
    for item, where, doc in actions:
        check("WS action", f"`{item}`", where, doc, needle=item)
    for item, where, doc in pushes:
        check("WS 消息类型", f"`{item}`", where, doc, needle=item)

    for item, where, doc in extract_accessors():
        name = item[:-2]  # 去掉 "()"，按裸函数名匹配
        check("单例访问器", item, where, doc, needle=name)

    for item, where, doc in extract_tools():
        name = item.split(":", 1)[1]
        check("LLM 工具", item, where, doc, needle=name)

    print("=" * 62)
    print("API 文档漂移校验  (规则见 docs/README.md「文档维护规则」)")
    print("=" * 62)

    if not DRIFT:
        print("✅ 全部通过：代码 API 面均已覆盖到对应文档。")
        return 0

    for kind, item, where, doc in sorted(DRIFT):
        print(f"✗ [{kind}] {item}")
        print(f"    代码: {where}")
        print(f"    应写入: docs/api/{doc}")
    print(f"\n共 {len(DRIFT)} 处漂移。请更新文档后重跑本命令。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
