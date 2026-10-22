# -*- coding: utf-8 -*-
"""会话准入：能不能在这个会话里说话。

权限源与主仓一致（memory/address_book.py 的三源口径）：
  1. memory.db 里 characters / groups 的 allow_conversation = 1
     → 取该实体 contacts 里的每个 session_id
  2. config/settings.py 的 SafetyConfig.CONVERSATION_ALLOWED_SESSIONS 白名单
  3. 保护模式 CONVERSATION_PROTECTION_MODE（只读出来当提示，不参与判定）

**默认全都不许。** 没在放行集合里的会话，发送命令一律拒绝——这是防越权的那道闸。
"""

from __future__ import annotations

import ast
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

MEMORY_DB = Path(r"E:\ABYSS\N_Touch\memory\memory.db")
CHAT_DB = Path(r"E:\ABYSS\N_Touch\memory\chat_history\chat_history.db")
SETTINGS = Path(r"E:\ABYSS\N_Touch\config\settings.py")

# 已开启的会话：能听也能说，长期有效，直到 leave_session 或主人收回。
# 它不是一个限时令牌，而是一次明确的确认动作。
GRANTS_FILE = Path(__file__).resolve().parent / "_run" / "grants.json"
# 待批队列：她自己批不了自己，只有主人（或主人的命令）能推进。
APPROVALS_FILE = Path(__file__).resolve().parent / "_run" / "approvals.json"


def confirmed() -> List[str]:
    if not GRANTS_FILE.exists():
        return []
    try:
        data = json.loads(GRANTS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return [s for s in data if isinstance(s, str)] if isinstance(data, list) else []


def revoke(session_id: str) -> bool:
    """退出会话：撤掉订阅与发言授权。"""
    sid = (session_id or "").strip()
    current = confirmed()
    if sid not in current:
        return False
    current.remove(sid)
    GRANTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    GRANTS_FILE.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


# ---------- 待批队列 ----------

def _load_approvals() -> List[Dict[str, Any]]:
    if not APPROVALS_FILE.exists():
        return []
    try:
        data = json.loads(APPROVALS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def _save_approvals(rows: List[Dict[str, Any]]) -> None:
    APPROVALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    APPROVALS_FILE.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def pending() -> List[Dict[str, Any]]:
    return [r for r in _load_approvals() if r.get("status") == "pending"]


MASTER_SESSION = "private_1787164785"
APPROVAL_TIMEOUT = 30.0  # 秒；跟主仓 Social Chat 一个尺度，等不到就当没批


def _max_seq(session_id: str) -> int:
    if not CHAT_DB.exists():
        return 0
    con = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
    try:
        row = con.execute(
            "select coalesce(max(seq), 0) from messages where session_id=?", (session_id,)
        ).fetchone()
        return int(row[0] or 0) if row else 0
    except sqlite3.Error:
        return 0
    finally:
        con.close()


def _replies_after(session_id: str, seq: int, limit: int = 3) -> List[Dict[str, str]]:
    """主人发出的、seq 之后的消息（她自己发的 user_id='system' 不算）。"""
    if not CHAT_DB.exists():
        return []
    con = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "select user_name, content_text from messages "
            "where session_id=? and seq>? and user_id<>'system' order by seq asc limit ?",
            (session_id, seq, limit),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    return [{"who": r[0] or "", "text": (r[1] or "").strip()} for r in rows]


def request(session_id: str, reason: str = "", text: str = "", sender=None) -> str:
    """把一次被拦下的动作弹给主人私聊，并挂进待批队列。"""
    sid = (session_id or "").strip()
    if not sid:
        raise ValueError("session_id 不能为空")
    rows = _load_approvals()
    for r in rows:
        if r.get("session_id") == sid and r.get("status") == "pending":
            return "这个会话的申请已经在等主人回了，别重复提。"
    seq = _max_seq(MASTER_SESSION)
    name = lookup_name(sid)
    body = (
        f"【审批】她请求进入 {lookup_name(sid) + '（' + sid + '）' if lookup_name(sid) and lookup_name(sid) != sid else sid}。\n"
        f"理由：{(reason or '没写').strip() if (reason or '').strip() else '没写'}\n"
        f"{('想发的话：' + text.strip()) if (text or '').strip() else ''}\n"
        f"回『同意』放行，回别的算拒绝；{int(APPROVAL_TIMEOUT)} 秒内没回就算了。"
    ).strip()
    sent = False
    if sender is not None:
        try:
            sender.send(MASTER_SESSION, body)
            sent = True
        except Exception:  # noqa: BLE001
            sent = False
    rows.append({
        "session_id": sid,
        "session_name": name,
        "reason": (reason or "").strip(),
        "blocked_text": (text or "").strip(),
        "status": "pending",
        "seq": seq,
        "ts": __import__("time").time(),
        "sent": sent,
    })
    _save_approvals(rows)
    tail = "已经弹到主人私聊了。" if sent else "（没连上发送通道，只记下来了）"
    return f"已提交审批，{tail}你现在进不去，等结果。"


def _set_status(session_id: str, status: str) -> None:
    rows = _load_approvals()
    for r in rows:
        if r.get("session_id") == session_id and r.get("status") == "pending":
            r["status"] = status
    _save_approvals(rows)


def poll() -> List[str]:
    """推进待批队列：主人回『同意』就放行，回别的算拒绝，超时算没批。

    返回要回灌给她的状态行——这样她永远知道自己站在哪一步，不会一头雾水。
    """
    import time as _t

    rows = _load_approvals()
    lines: List[str] = []
    changed = False
    for r in rows:
        if r.get("status") != "pending":
            continue
        sid = r.get("session_id", "")
        name = r.get("session_name") or sid
        replies = _replies_after(MASTER_SESSION, int(r.get("seq", 0)), 1)
        if replies:
            said = replies[0]["text"]
            if "同意" in said:
                r["status"] = "approved"
                current = confirmed()
                if sid not in current:
                    current.append(sid)
                    GRANTS_FILE.parent.mkdir(parents=True, exist_ok=True)
                    GRANTS_FILE.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
                lines.append(f"【审批】{name}：主人同意了，现在可以 enter_session 进去。")
            else:
                r["status"] = "rejected"
                lines.append(f"【审批】{name}：主人没同意（回了「{said[:20]}」），别再试这个会话。")
            changed = True
        elif _t.time() - float(r.get("ts", 0)) > APPROVAL_TIMEOUT:
            r["status"] = "expired"
            lines.append(
                f"【审批】{name}：主人 {int(APPROVAL_TIMEOUT)} 秒内没回，这次没批。"
                "别等，也别反复提；要走就走别的路。"
            )
            changed = True
    if changed:
        _save_approvals(rows)
    return lines


def approve(session_id: str) -> str:
    """主人批：该会话长期可听可说。注意这只能由主人侧调用。"""
    sid = (session_id or "").strip()
    rows = _load_approvals()
    hit = None
    for r in rows:
        if r.get("session_id") == sid and r.get("status") == "pending":
            hit = r
            break
    if hit is None:
        return f"没有待批的 {sid}"
    hit["status"] = "approved"
    _save_approvals(rows)
    current = confirmed()
    if sid not in current:
        current.append(sid)
        GRANTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        GRANTS_FILE.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return f"已批准 {sid}（{hit.get('session_name') or ''}）；被拦下的那句话作废，不会补发。"


def reject(session_id: str) -> str:
    sid = (session_id or "").strip()
    rows = _load_approvals()
    hit = None
    for r in rows:
        if r.get("session_id") == sid and r.get("status") == "pending":
            hit = r
            break
    if hit is None:
        return f"没有待批的 {sid}"
    hit["status"] = "rejected"
    _save_approvals(rows)
    return f"已驳回 {sid}"


def confirm(session_id: str) -> bool:
    """记下"已获准进入"。幂等：重复申请不会重复写。返回是否新写入。"""
    sid = (session_id or "").strip()
    if not sid:
        raise ValueError("session_id 不能为空")
    current = confirmed()
    if sid in current:
        return False
    current.append(sid)
    GRANTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    GRANTS_FILE.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def is_confirmed(session_id: str) -> bool:
    return (session_id or "").strip() in confirmed()


def _from_memory_db() -> Dict[str, str]:
    """allow_conversation=1 的实体 → 它的 contacts 里的 session_id。"""
    out: Dict[str, str] = {}
    if not MEMORY_DB.exists():
        return out
    con = sqlite3.connect(f"file:{MEMORY_DB}?mode=ro", uri=True)
    try:
        for table in ("characters", "groups"):
            for name, contacts, allow in con.execute(
                f"select name, contacts, allow_conversation from {table}"
            ):
                if not allow:
                    continue
                for c in json.loads(contacts or "[]"):
                    sid = (c or {}).get("session_id") or ""
                    if sid:
                        out[sid] = f"{name}（{table[:-1]}）"
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return out


def _from_settings() -> List[str]:
    """settings.py 里的白名单常量（按字面量解析，不 import 整套配置）。"""
    if not SETTINGS.exists():
        return []
    text = SETTINGS.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"CONVERSATION_ALLOWED_SESSIONS\s*=\s*(\[[^\]]*\])", text)
    if not m:
        return []
    try:
        val = ast.literal_eval(m.group(1))
    except (ValueError, SyntaxError):
        return []
    return [v for v in val if isinstance(v, str)]


def _session_names() -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not CHAT_DB.exists():
        return out
    con = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
    try:
        for sid, sname in con.execute("select session_id, session_name from sessions"):
            out[sid] = sname or ""
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return out


def allowed() -> Dict[str, str]:
    """session_id → 放行说明。默认空集合 = 哪儿都不许说话。"""
    table = _from_memory_db()
    names = _session_names()
    for sid in _from_settings():
        table.setdefault(sid, "配置白名单")
    # 白名单里的会话可能没有 contacts 记录，名字从会话表补
    out = {}
    for sid, why in table.items():
        nm = names.get(sid, "")
        out[sid] = f"{why}｜{nm}" if nm else why
    return out


def protection_on() -> bool:
    if not SETTINGS.exists():
        return True
    text = SETTINGS.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"CONVERSATION_PROTECTION_MODE\s*=\s*(True|False)", text)
    return (m.group(1) == "True") if m else True


def is_allowed(session_id: str) -> bool:
    return (session_id or "").strip() in allowed()


def allowed_list_text() -> str:
    table = allowed()
    if not table:
        return "（没有任何会话被放行）"
    return "\n".join(f"- {sid}  ← {why}" for sid, why in sorted(table.items()))


def lookup_name(session_id: str) -> str:
    return _session_names().get((session_id or "").strip(), "")
