"""Durable bridge inbox, generation/cursor pair, and local send journal.

Every operation opens a short SQLite transaction. No credentials are stored here.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .contract import ContractError, PollBatch, SendReceipt, nonempty, valid_connection_id


class BridgeState:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS connections (
                    connection_id TEXT PRIMARY KEY, platform TEXT NOT NULL,
                    generation TEXT NOT NULL DEFAULT '', cursor INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS inbox (
                    connection_id TEXT NOT NULL, generation TEXT NOT NULL,
                    session_id TEXT NOT NULL, message_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL, payload TEXT NOT NULL,
                    state TEXT NOT NULL, reason TEXT NOT NULL,
                    PRIMARY KEY(connection_id, generation, session_id, message_id)
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    request_id TEXT PRIMARY KEY, connection_id TEXT NOT NULL,
                    logical_id TEXT NOT NULL, generation TEXT NOT NULL,
                    conversation TEXT NOT NULL, text TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'prepared', send_id TEXT, message_id TEXT,
                    created_at INTEGER NOT NULL,
                    UNIQUE(connection_id, logical_id)
                );
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def bind(self, connection_id: str, platform: str):
        valid_connection_id(connection_id)
        nonempty(platform, "platform")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO connections(connection_id,platform) VALUES (?,?)",
                       (connection_id, platform))
            row = db.execute("SELECT platform FROM connections WHERE connection_id=?",
                             (connection_id,)).fetchone()
            if row["platform"] != platform:
                raise ContractError("a connection_id cannot silently change platform")

    def checkpoint(self, connection_id: str) -> tuple[str, int]:
        with self._db() as db:
            row = db.execute("SELECT generation,cursor FROM connections WHERE connection_id=?",
                             (connection_id,)).fetchone()
            if row is None:
                raise ContractError("unknown connection")
            return row["generation"], row["cursor"]

    def commit_poll(self, connection_id: str, batch: PollBatch, expected: tuple[str, int]):
        """Stage all raw rows and advance progress in one transaction, including quarantines."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT generation,cursor FROM connections WHERE connection_id=?",
                             (connection_id,)).fetchone()
            if row is None or (row["generation"], row["cursor"]) != expected:
                raise ContractError("checkpoint changed concurrently; discard this page")
            if row["generation"] == batch.generation and batch.cursor < row["cursor"]:
                raise ContractError("cursor cannot go backwards within one generation")
            for msg in sorted(batch.messages, key=lambda item: item.sequence):
                encoded = json.dumps(msg.payload, ensure_ascii=False, sort_keys=True)
                old = db.execute(
                    "SELECT payload FROM inbox WHERE connection_id=? AND generation=? "
                    "AND session_id=? AND message_id=?",
                    (connection_id, batch.generation, msg.session_id, msg.message_id),
                ).fetchone()
                if old is not None and old["payload"] != encoded:
                    raise ContractError("an immutable message changed after publication")
                db.execute("INSERT OR IGNORE INTO inbox VALUES (?,?,?,?,?,?,?,?)", (
                    connection_id, batch.generation, msg.session_id, msg.message_id, msg.sequence,
                    encoded, "quarantined" if msg.quarantine_reason else "pending",
                    msg.quarantine_reason,
                ))
            db.execute("UPDATE connections SET generation=?,cursor=? WHERE connection_id=?",
                       (batch.generation, batch.cursor, connection_id))

    def inbox(self, connection_id: str, state: str = "pending") -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM inbox WHERE connection_id=? AND state=? "
                              "ORDER BY rowid LIMIT 100", (connection_id, state)).fetchall()
            return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def acknowledge(self, connection_id: str, generation: str, session_id: str, message_id: str):
        """Call only after durable downstream storage; quarantine cannot be acknowledged here."""
        with self._db() as db:
            db.execute("UPDATE inbox SET state='done' WHERE connection_id=? AND generation=? "
                       "AND session_id=? AND message_id=? AND state='pending'",
                       (connection_id, generation, session_id, message_id))

    def prepare_send(self, connection_id: str, logical_id: str, conversation: str, text: str) -> dict:
        nonempty(logical_id, "logical_id")
        nonempty(text, "text")
        sid = nonempty(conversation, "conversation")
        scope, sep, target = sid.partition("_")
        if scope not in ("private", "group") or not sep or not target:
            raise ContractError("send target must be a known private/group session")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            conn = db.execute("SELECT generation FROM connections WHERE connection_id=?",
                              (connection_id,)).fetchone()
            if conn is None or not conn["generation"]:
                raise ContractError("poll the bridge before preparing a send")
            old = db.execute("SELECT * FROM outbox WHERE connection_id=? AND logical_id=?",
                             (connection_id, logical_id)).fetchone()
            if old is not None:
                if old["conversation"] != conversation or old["text"] != text:
                    raise ContractError("logical_id already belongs to another payload")
                return dict(old)
            request_id = uuid.uuid4().hex
            db.execute("INSERT INTO outbox(request_id,connection_id,logical_id,generation,"
                       "conversation,text,created_at) VALUES (?,?,?,?,?,?,?)", (
                           request_id, connection_id, logical_id, conn["generation"],
                           conversation, text, int(time.time()),
                       ))
            return dict(db.execute("SELECT * FROM outbox WHERE request_id=?", (request_id,)).fetchone())

    def send_record(self, request_id: str) -> dict:
        with self._db() as db:
            row = db.execute("SELECT * FROM outbox WHERE request_id=?", (request_id,)).fetchone()
            if row is None:
                raise ContractError("unknown local request_id")
            return dict(row)

    def claim_send(self, connection_id: str, request_id: str) -> bool:
        """Persist 'submitting' before the only POST; concurrent callers cannot double-submit."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            return bool(db.execute("UPDATE outbox SET status='submitting' WHERE request_id=? "
                                   "AND connection_id=? AND status='prepared'",
                                   (request_id, connection_id)).rowcount)

    def record_receipt(self, connection_id: str, receipt: SendReceipt):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,send_id,message_id FROM outbox WHERE request_id=? AND connection_id=?",
                             (receipt.request_id, connection_id)).fetchone()
            if row is None:
                raise ContractError("receipt belongs to another connection")
            if row["send_id"] is not None and row["send_id"] != receipt.send_id:
                raise ContractError("bridge changed the receipt identity")
            # A late HTTP response cannot downgrade an already observed/failed record.
            if row["status"] in ("observed", "rejected", "failed"):
                if row["status"] != receipt.status or row["message_id"] != receipt.message_id:
                    raise ContractError("terminal send receipt changed")
                return
            if row["status"] == "actioned" and receipt.status == "queued":
                return
            db.execute("UPDATE outbox SET status=?,send_id=?,message_id=? WHERE request_id=?",
                       (receipt.status, receipt.send_id, receipt.message_id, receipt.request_id))

    def mark_unknown(self, connection_id: str, request_id: str):
        with self._db() as db:
            db.execute("UPDATE outbox SET status='unknown' WHERE request_id=? AND connection_id=? "
                       "AND status NOT IN ('observed','rejected','failed')", (request_id, connection_id))

    def mark_rejected(self, connection_id: str, request_id: str):
        """确定从未提交到桥接（接入服务未认领）时置 rejected，避免留下永远不发的 prepared。"""
        with self._db() as db:
            db.execute("UPDATE outbox SET status='rejected' WHERE request_id=? AND connection_id=? "
                       "AND status='prepared'", (request_id, connection_id))

    def pending_sends(self, connection_id: str) -> list[dict]:
        with self._db() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM outbox WHERE connection_id=? AND status IN "
                "('submitting','queued','actioned','unknown') ORDER BY created_at DESC LIMIT 50",
                (connection_id,)).fetchall()]

    def summary(self, connection_id: str) -> dict:
        with self._db() as db:
            counts = {row["state"]: row["n"] for row in db.execute(
                "SELECT state,COUNT(*) n FROM inbox WHERE connection_id=? GROUP BY state",
                (connection_id,)).fetchall()}
            sends = {row["status"]: row["n"] for row in db.execute(
                "SELECT status,COUNT(*) n FROM outbox WHERE connection_id=? GROUP BY status",
                (connection_id,)).fetchall()}
        return {"inbox": counts, "outbox": sends}
