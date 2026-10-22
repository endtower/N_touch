# -*- coding: utf-8 -*-
"""模型适配 + 输出协议校验 + 泄露检测。

本实验的隔离靠字段，所以校验和检测不是可选项，是设计的一部分：
协议不合格直接 raise（fail-fast，和 main 的硬约定一致），不静默容错。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

_CONFIDENCE = {"high", "medium", "low"}
_LEAK_LEADS = ("我准备", "我的目的", "策略", "先试探", "计划是", "为了让他", "我的算盘")


class ProtocolError(ValueError):
    """模型输出不符合双产物协议。"""


def parse_output(raw: str) -> Dict[str, Any]:
    if not isinstance(raw, str) or not raw.strip():
        raise ProtocolError("模型没有输出")
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"输出不是合法 JSON: {exc}") from exc

    inner = data.get("inner")
    if not isinstance(inner, dict):
        raise ProtocolError("缺少 inner")
    for key in ("read", "want"):
        if not str(inner.get(key, "")).strip():
            raise ProtocolError(f"inner.{key} 不能为空")
    # 中文档位是同一件事的常见写法，归一化而不是报错
    conf = str(inner.get("confidence", "")).strip().lower()
    conf = {"高": "high", "中": "medium", "低": "low", "中等": "medium", "较高": "high", "较低": "low"}.get(conf, conf)
    if conf not in _CONFIDENCE:
        # 把握度只用来调预算，不值得让整轮失败；不明就当中等
        conf = "medium"
    inner["confidence"] = conf

    speech = data.get("speech", "")
    if speech is None:
        speech = ""
    if not isinstance(speech, str):
        raise ProtocolError("speech 必须是字符串")

    act = data.get("act")
    if act is not None:
        if not isinstance(act, dict) or not act.get("tool"):
            raise ProtocolError("act 必须是 {tool, args} 或 null")
        act.setdefault("args", {})

    return {"inner": inner, "speech": speech, "act": act}


def _bigrams(text: str) -> set:
    clean = re.sub(r"[\s\W]+", "", text or "")
    return {clean[i : i + 2] for i in range(len(clean) - 1)}


def leak_check(inner: Dict[str, str], speech: str, threshold: float = 0.34) -> Dict[str, Any]:
    """inner 与 speech 的字面重叠度 + 显式策略词。

    这是启发式，不是判据——它的用途是把可疑的那几句挑出来给人读，
    而不是自动拦截。
    """
    if not speech:
        return {"score": 0.0, "hit_words": [], "suspicious": False}
    inner_text = " ".join(str(v) for v in inner.values() if v)
    a, b = _bigrams(inner_text), _bigrams(speech)
    score = len(a & b) / max(1, len(b))
    hits = [w for w in _LEAK_LEADS if w in speech]
    return {"score": round(score, 3), "hit_words": hits, "suspicious": score >= threshold or bool(hits)}


class MockModel:
    """离线验证用：按剧本吐出协议输出。网络恢复后替换成真模型，其余代码不动。"""

    def __init__(self, script: List[Dict[str, Any]]):
        self._script = list(script)
        self.calls: List[Dict[str, Any]] = []

    def complete(self, messages: List[Dict[str, str]], tools: Optional[List[Dict]] = None) -> str:
        self.calls.append({"messages": messages, "tools": tools or []})
        if not self._script:
            return json.dumps(
                {
                    "inner": {"read": "没什么可说的了", "want": "停下", "fear": "多话", "confidence": "high"},
                    "speech": "",
                    "act": None,
                },
                ensure_ascii=False,
            )
        return json.dumps(self._script.pop(0), ensure_ascii=False)
