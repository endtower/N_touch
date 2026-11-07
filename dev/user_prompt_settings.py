"""用户专属的系统人格设置；不属于内心人格注册表，不提供 LLM 工具入口。"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from paths import data_root

_TARGETS = {
    "pfca": ("PFCA", "追加到 PFCA 系统提示词末尾"),
    "reflection": ("反思系统", "追加到反思系统提示词末尾"),
}
_WRITE_LOCK = threading.Lock()
_MAX_PROMPT_LENGTH = 50000


def _path() -> Path:
    return data_root() / "user_settings" / "system_personas.json"


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("系统人格设置必须是 JSON 对象")
    for target in _TARGETS:
        if target in document and not isinstance(document[target], str):
            raise ValueError(f"{target} 系统人格正文必须是文字")
    return document


def _entry(target: str, text: str) -> dict:
    name, scene = _TARGETS[target]
    return {"persona_name": f"system:{target}", "display_name": name,
            "scene": scene, "kernel_prompt": text, "user_only": True}


def list_user_system_personas() -> list[dict]:
    """仅供人工维护 HTTP 页面展示；读取不会播种文件或改动内心人格。"""
    document = _read(_path())
    return [_entry(target, document.get(target, "")) for target in _TARGETS]


def append_user_system_persona(base_prompt: str, target: str) -> str:
    """每次组装时读取最新正文，只追加文本，不向模型提供设置元数据或操作入口。"""
    if target not in _TARGETS:
        raise ValueError("不支持的系统人格目标")
    text = _read(_path()).get(target, "").strip()
    return f"{base_prompt.rstrip()}\n\n{text}" if text else base_prompt


def _update(target: str, text: str) -> dict:
    with _WRITE_LOCK:
        path = _path()
        document = _read(path)
        if document.get(target, "") == text:
            return _entry(target, text)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            history = path.parent / "history"
            history.mkdir(exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            (history / f"system_personas_{stamp}.json").write_bytes(path.read_bytes())
        document[target] = text  # 保留另一个系统及未知字段。
        fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(document, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return _entry(target, text)


async def update_user_system_persona(persona_name: str, changes: dict) -> dict:
    """仅由人工维护 HTTP 入口调用；只允许编辑正文，空串清除补充设定。"""
    target = persona_name.removeprefix("system:")
    if persona_name != f"system:{target}" or target not in _TARGETS:
        raise ValueError("不支持的系统人格目标")
    if not isinstance(changes, dict) or set(changes) != {"kernel_prompt"}:
        raise ValueError("系统人格只允许修改补充人格设定")
    text = changes["kernel_prompt"]
    if not isinstance(text, str):
        raise ValueError("补充人格设定必须是文字")
    text = text.strip()
    if len(text) > _MAX_PROMPT_LENGTH:
        raise ValueError(f"补充人格设定最多 {_MAX_PROMPT_LENGTH} 字")
    return await asyncio.to_thread(_update, target, text)
