# -*- coding: utf-8 -*-
"""真模型适配器：绕过坏掉的系统 DNS。

这台机器现在所有域名都被解析到 198.18.x.x（代理 fake-ip，上游不通），
但真实 IP 的 443 是通的。所以这里用 DoH 拿真 IP，再用 curl --resolve 钉住它连。
等 DNS 修好，把 _pinned() 里的 --resolve 去掉即可，其余不动。

协议：不用 function calling。工具体现在 JSON 的 act 字段里，由 loop 自己派发。
这样少一层序列化，也能让"内心/正文"和动作在同一次输出里保持一起。
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

DOH_PIN = "dns.alidns.com:443:223.5.5.5"
DOH_URL = "https://dns.alidns.com/resolve?name={host}&type=A"
ENV_PATH = Path(r"E:\ABYSS\N_Touch\.env.dev")


def _env(name: str, default: str = "") -> str:
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip()
    return os.environ.get(name, default)


class LiveModel:
    """OpenAI 兼容端点 + DoH 固定 IP。"""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        max_tokens: int = 3000,
        temperature: float = 0.8,
        timeout: int = 90,
    ):
        self.base_url = (base_url or _env("FLASH_BASE_URL") or "https://ark.cn-beijing.volces.com/api/plan/v3").rstrip("/")
        self.api_key = api_key or _env("FLASH_API_KEY")
        self.model = model or _env("FLASH_MODEL") or "DeepSeek-V4.1-Flash"
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self.host = self.base_url.split("://", 1)[1].split("/", 1)[0]
        self._ip: Optional[str] = None
        self._ip_at = 0.0
        self.calls: List[Dict[str, Any]] = []

    # ---------- 解析 ----------

    def _resolve(self) -> str:
        if self._ip and time.time() - self._ip_at < 600:
            return self._ip
        out = subprocess.run(
            [
                "curl", "-s", "--insecure", "--max-time", "12",
                "--resolve", DOH_PIN,
                DOH_URL.format(host=self.host),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        ).stdout
        try:
            answers = json.loads(out)["Answer"]
            ip = next(a["data"] for a in answers if a.get("type") == 1)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"DoH 解析失败 {self.host}: {exc} / {out[:200]}") from exc
        self._ip, self._ip_at = ip, time.time()
        return ip

    # ---------- 调用 ----------

    def complete(self, messages: List[Dict[str, str]], tools: Optional[List[Dict]] = None) -> str:
        """tools 参数故意忽略：本实验用 JSON 的 act 字段代替 function calling。"""
        ip = self._resolve()
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        tmp = Path(tempfile.gettempdir()) / "unified_body.json"
        tmp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")

        proc = subprocess.run(
            [
                "curl", "-s", "--max-time", str(self.timeout),
                "--resolve", f"{self.host}:443:{ip}",
                "-H", f"Authorization: Bearer {self.api_key}",
                "-H", "Content-Type: application/json",
                "--data-binary", f"@{tmp}",
                f"{self.base_url}/chat/completions",
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        raw = (proc.stdout or "").strip()
        self.calls.append({"messages": len(messages), "raw_len": len(raw)})
        if not raw:
            raise RuntimeError(f"模型无响应（curl exit={proc.returncode}）: {proc.stderr[:300]}")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"模型响应不是 JSON: {raw[:300]}") from exc
        if "error" in data:
            raise RuntimeError(f"模型报错: {json.dumps(data['error'], ensure_ascii=False)[:300]}")

        msg = data["choices"][0]["message"]
        content = (msg.get("content") or "").strip()
        if not content:
            reasoning = (msg.get("reasoning_content") or "").strip()
            raise RuntimeError(f"模型只出了思考没出正文（可能 max_tokens 太小）: {reasoning[:200]}")
        return content
