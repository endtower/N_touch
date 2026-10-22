# -*- coding: utf-8 -*-
"""一条命令发一句话 / 一张图 / 一个戳。

底层两个口子（都是本机，不需要 DNS）：
  POST /v1/send        {scope, target_id, segments}   —— 文字、图片
  POST /v1/onebot-api  {action, params}               —— 戳一戳等任意 OneBot 动作

命令行：
  python -m unified.qq_cli text  group_1095799561 "在呢"
  python -m unified.qq_cli image group_1095799561 pic.jpg
  python -m unified.qq_cli poke  group_1095799561 1787164785
  python -m unified.qq_cli poke  private_1787164785          # 私聊戳，目标即对方
  任意命令加 --dry 只打印不发送。
"""

from __future__ import annotations

import json
import shlex
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Tuple

TOKEN_PATH = Path(r"E:\ABYSS\N_Touch\manager\service_token.txt")
BASE = "http://127.0.0.1:5003"


def _token() -> str:
    return TOKEN_PATH.read_text(encoding="utf-8").strip()


def _post(path: str, body: Dict[str, Any], timeout: int = 20) -> Dict[str, Any]:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Manager-Token": _token()},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


def parse_target(target: str) -> Tuple[str, str]:
    """group_xxx / private_xxx / g:xxx / p:xxx → (scope, id)"""
    t = (target or "").strip()
    for sep in ("_", ":"):
        if sep in t:
            head, tail = t.split(sep, 1)
            head = {"g": "group", "group": "group", "p": "private", "private": "private"}.get(head.lower(), head.lower())
            if head in ("group", "private") and tail:
                return head, tail
    raise ValueError(f"目标格式不对：{target!r}（用 group_123 / private_456）")


def send_text(target: str, text: str, dry: bool = False) -> str:
    scope, tid = parse_target(target)
    if not text.strip():
        raise ValueError("内容为空")
    if dry:
        return f"[dry] text → {scope}/{tid}: {text}"
    _post("/v1/send", {"scope": scope, "target_id": tid,
                       "segments": [{"type": "text", "data": {"text": text}}]})
    return f"已发送到 {scope}/{tid}"


def send_image(target: str, filename: str, dry: bool = False) -> str:
    scope, tid = parse_target(target)
    if not filename.strip():
        raise ValueError("文件名为空")
    if dry:
        return f"[dry] image → {scope}/{tid}: {filename}"
    _post("/v1/send", {"scope": scope, "target_id": tid,
                       "segments": [{"type": "image", "data": {"file": filename}}]})
    return f"已发送图片 {filename} 到 {scope}/{tid}"


def poke(target: str, user_id: str = "", dry: bool = False) -> str:
    scope, tid = parse_target(target)
    if scope == "group":
        if not user_id:
            raise ValueError("群戳需要 user_id")
        action, params = "group_poke", {"group_id": int(tid), "user_id": int(user_id)}
    else:
        action, params = "friend_poke", {"user_id": int(tid)}
    if dry:
        return f"[dry] {action} {params}"
    _post("/v1/onebot-api", {"action": action, "params": params})
    return f"已戳 {params}"


VERBS = {"text": send_text, "image": send_image, "poke": poke}


def run_command(line: str, dry: bool = False, guard=None) -> str:
    """给 LLM 用：把 'qq text group_1 你好' 这种一行命令执行掉。

    guard：发送前的准入检查，接到目标 session_id；不放行就由它抛错。
    """
    parts = shlex.split(line.strip())
    if parts and parts[0] == "qq":
        parts = parts[1:]
    if not parts:
        raise ValueError("空命令")
    verb, args = parts[0], parts[1:]
    if verb not in VERBS:
        raise ValueError(f"不认识的动作：{verb}（可用：text / image / poke）")
    if guard is not None:
        if not args:
            raise ValueError("缺少目标会话")
        guard(args[0], line.strip())
    return VERBS[verb](*args, dry=dry)


def main(argv: List[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    dry = "--dry" in argv
    argv = [a for a in argv if a != "--dry"]
    if len(argv) < 2:
        print(__doc__)
        return 2
    verb, rest = argv[0], argv[1:]
    if verb not in VERBS:
        print(f"不认识的动作：{verb}")
        return 2
    try:
        print(VERBS[verb](*rest, dry=dry))
    except Exception as exc:  # noqa: BLE001
        print(f"失败：{type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
