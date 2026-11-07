"""Strict wire validation for the proposed bridge v0 contract (no network or disk I/O)."""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any


PROTOCOL_VERSION = "0"
REQUIRED_CAPABILITIES = frozenset({
    "text.receive", "text.send", "send.idempotency.persistent", "message.direction.final",
})
SEND_STATES = frozenset({"queued", "actioned", "observed", "rejected", "failed", "unknown"})


class ContractError(ValueError):
    """The peer did not satisfy the contract; processing must stop without sending."""


def nonempty(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field} must be a nonempty string")
    return value


def integer(value: Any, field: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ContractError(f"{field} must be an integer >= {minimum}")
    return value


def object_value(value: Any, field: str) -> dict:
    if not isinstance(value, dict):
        raise ContractError(f"{field} must be an object")
    return value


def validate_health(body: Any, platform: str) -> dict:
    body = object_value(body, "health")
    if body.get("protocol_version") != PROTOCOL_VERSION or body.get("platform") != platform:
        raise ContractError("bridge protocol or platform mismatch")
    nonempty(body.get("generation"), "generation")
    caps = body.get("capabilities")
    if not isinstance(caps, list) or not all(isinstance(c, str) for c in caps):
        raise ContractError("capabilities must be an array of strings")
    if not REQUIRED_CAPABILITIES.issubset(caps):
        raise ContractError("bridge is missing required v0 capabilities")
    if body.get("ready") is not True:
        raise ContractError("bridge is not ready")
    return body


@dataclass(frozen=True)
class BridgeMessage:
    session_id: str
    message_id: str
    sequence: int
    payload: dict
    quarantine_reason: str


@dataclass(frozen=True)
class PollBatch:
    generation: str
    cursor: int
    messages: tuple[BridgeMessage, ...]
    raw: dict


def parse_poll(body: Any, after: int, platform: str) -> PollBatch:
    body = object_value(body, "poll")
    if body.get("protocol_version") != PROTOCOL_VERSION or body.get("platform") != platform:
        raise ContractError("poll protocol or platform mismatch")
    generation = nonempty(body.get("generation"), "generation")
    cursor = integer(body.get("cursor"), "cursor")
    if cursor < after:
        raise ContractError("cursor went backwards within the requested generation")
    if body.get("arrival_confirmed") is not False:
        raise ContractError("v0 cannot promise complete arrival")
    if body.get("mode") not in ("patrol", "focus", "hold") or not isinstance(body.get("focus"), list):
        raise ContractError("poll must expose mode and focus")
    sessions = body.get("sessions")
    if not isinstance(sessions, list):
        raise ContractError("sessions must be an array")
    messages = []
    seen = set()
    for session in sessions:
        session = object_value(session, "session")
        info = object_value(session.get("session_info"), "session_info")
        sid = nonempty(info.get("session_id"), "session_id")
        scope, sep, identity = sid.partition("_")
        if not sep or not identity or scope not in ("private", "group", "unknown"):
            raise ContractError("session_id must retain its explicit scope")
        rows = session.get("message_list")
        if not isinstance(rows, list):
            raise ContractError("message_list must be an array")
        for row in rows:
            row = object_value(row, "message")
            mid = nonempty(row.get("message_id"), "message_id")
            seq = integer(row.get("sequence"), "sequence", 1)
            if not after < seq <= cursor or seq in seen:
                raise ContractError("message sequence outside page or repeated")
            seen.add(seq)
            timestamp = integer(row.get("timestamp"), "timestamp")
            if row.get("timestamp_source") != "observed_at":
                raise ContractError("v0 timestamp must be labelled observed_at")
            direction = row.get("direction")
            if direction not in ("inbound", "outbound", "unknown"):
                raise ContractError("direction must be explicit before publishing a row")
            source = row.get("news_source")
            if source not in ("diff", "badge", "unquantified", "baseline"):
                raise ContractError("news_source must be attached to every message")
            text = row.get("content_text")
            if not isinstance(text, str):
                raise ContractError("content_text must be a string")
            uid = row.get("user_id")
            if not isinstance(uid, str):
                raise ContractError("user_id must be a string (empty is allowed)")
            if row.get("is_own") is not (direction == "outbound"):
                raise ContractError("is_own must agree with direction")
            if direction == "inbound" and uid in ("self", "system"):
                raise ContractError("inbound rows cannot identify the sender as self/system")
            reasons = []
            if scope == "unknown":
                reasons.append("unknown_scope")
            if direction == "unknown":
                reasons.append("unknown_direction")
            if source in ("baseline", "unquantified"):
                reasons.append("uncertain_observation")
            payload = {
                "bridge_origin": {
                    "platform": platform, "generation": generation,
                    "session_info": copy.deepcopy(info), "message": copy.deepcopy(row),
                },
                "session_info": {**info, "platform": platform},
                "message_list": [{
                    **row, "timestamp": timestamp,
                    "user_id": "system" if direction == "outbound" else uid,
                    "content": [{"type": "text", "data": {"text": text}}],
                }],
            }
            messages.append(BridgeMessage(sid, mid, seq, payload, ",".join(reasons)))
    return PollBatch(generation, cursor, tuple(messages), body)


@dataclass(frozen=True)
class SendReceipt:
    """A local request and its bridge receipt; message_id is absent until observed."""
    request_id: str
    status: str
    send_id: str | None = None
    message_id: str | None = None

    @classmethod
    def parse(cls, body: Any, request_id: str) -> SendReceipt:
        body = object_value(body, "send receipt")
        if body.get("request_id") != request_id or body.get("status") not in SEND_STATES:
            raise ContractError("send receipt identity or status mismatch")
        send_id = nonempty(body.get("send_id"), "send_id")
        mid = body.get("message_id")
        if mid is not None:
            nonempty(mid, "message_id")
        if body["status"] == "observed" and mid is None:
            raise ContractError("observed must include message_id")
        if body["status"] != "observed" and mid is not None:
            raise ContractError("only observed receipts can claim message_id")
        if body["status"] == "actioned":
            report = object_value(body.get("report"), "report")
            if report.get("clicked") is not True:
                raise ContractError("actioned requires report.clicked=true")
        return cls(request_id, body["status"], send_id, mid)


def valid_connection_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
        raise ContractError("connection_id must be 1-64 ASCII letters/digits/underscore/hyphen")
    return value
