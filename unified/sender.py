# -*- coding: utf-8 -*-
"""出站：走 ingress 的本机 API（127.0.0.1:5003），不碰 QQ 协议、不需要 DNS。

POST /v1/send
  headers: X-Manager-Token
  body: {scope, target_id, segments}
ingress 发完会自己落库，所以历史里能立刻看到她说的话。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict

TOKEN_PATH = Path(r"E:\ABYSS\N_Touch\manager\service_token.txt")


class Sender:
    def __init__(self, url: str = "http://127.0.0.1:5003", token: str | None = None, dry: bool = True):
        self.url = url.rstrip("/")
        self.token = token or TOKEN_PATH.read_text(encoding="utf-8").strip()
        self.dry = dry
        self.sent: list[Dict[str, Any]] = []

    def health(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.url}/health", timeout=5) as resp:
                return resp.status == 200
        except Exception:  # noqa: BLE001
            return False

    def send(self, session_id: str, text: str) -> Dict[str, Any]:
        scope, target = session_id.split("_", 1)
        body = {
            "scope": scope,
            "target_id": target,
            "segments": [{"type": "text", "data": {"text": text}}],
        }
        if self.dry:
            self.sent.append({"dry": True, "session": session_id, "text": text})
            return {"dry": True}

        req = urllib.request.Request(
            f"{self.url}/v1/send",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Manager-Token": self.token},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"发送失败 HTTP {exc.code}: {detail}") from exc
        self.sent.append({"dry": False, "session": session_id, "text": text, "result": result})
        return result
