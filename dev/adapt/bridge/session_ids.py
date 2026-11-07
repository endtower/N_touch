"""Persistent numeric WeChat session addresses; remote identity stays in the database."""

from contextlib import closing
import re
import sqlite3


def _schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS bridge_session_ids (
        number INTEGER PRIMARY KEY AUTOINCREMENT,
        platform TEXT NOT NULL,
        connection_id TEXT NOT NULL,
        generation TEXT NOT NULL,
        remote_session_id TEXT NOT NULL,
        scope TEXT NOT NULL,
        UNIQUE(platform, connection_id, generation, remote_session_id)
    )""")
    row = db.execute("SELECT seq FROM sqlite_sequence WHERE name='bridge_session_ids'").fetchone()
    if row is None:
        db.execute("INSERT INTO sqlite_sequence(name,seq) VALUES ('bridge_session_ids',10000)")
    elif row[0] < 10000:
        db.execute("UPDATE sqlite_sequence SET seq=10000 WHERE name='bridge_session_ids'")


def _resolve(db, connection_id, generation, remote_id, platform="wechat", number=None):
    scope, separator, identity = remote_id.partition("_")
    if platform != "wechat" or not connection_id or not generation or not separator \
            or not identity or scope not in ("private", "group", "unknown"):
        raise ValueError("complete WeChat source identity is required")
    source = (platform, connection_id, generation, remote_id)
    row = db.execute("SELECT number,scope FROM bridge_session_ids WHERE platform=? "
                     "AND connection_id=? AND generation=? AND remote_session_id=?", source).fetchone()
    if row is None:
        cursor = db.execute("INSERT INTO bridge_session_ids "
                            "(number,platform,connection_id,generation,remote_session_id,scope) "
                            "VALUES (?,?,?,?,?,?)", (number, *source, scope))
        row = (cursor.lastrowid, scope)
    if number is not None and row[0] != number:
        raise ValueError("existing numeric session mapping conflicts with stored route")
    return f"{platform}_{row[1]}_{row[0]}"


def resolve_session_id(path, connection_id, generation, remote_id):
    """Allocate once per connection/generation/remote target, including across restarts."""
    with closing(sqlite3.connect(str(path), timeout=10)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        _schema(db)
        return _resolve(db, connection_id, generation, remote_id)


def migrate_existing(path):
    """Rename existing WeChat sessions, messages, cursors and routes in one transaction.

    The caller stops Core/ingress and backs up SQLite before invoking this function.
    Message sequence numbers, remote IDs, timestamps and bodies are untouched.
    """
    changes = {}
    with closing(sqlite3.connect(str(path), timeout=10)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        _schema(db)
        routes = db.execute("SELECT session_id,connection_id,generation,remote_session_id "
                            "FROM session_routes WHERE platform='wechat' ORDER BY rowid").fetchall()
        for old, connection, generation, remote in routes:
            numeric = re.fullmatch(r"wechat_(private|group|unknown)_([0-9]+)", old)
            number = int(numeric.group(2)) if numeric else None
            if numeric and numeric.group(1) != remote.partition("_")[0]:
                raise ValueError("stored session scope does not match its route")
            new = _resolve(db, connection, generation, remote, number=number)
            if old == new:
                continue
            if db.execute("SELECT 1 FROM sessions WHERE session_id=?", (new,)).fetchone():
                raise ValueError("numeric address already belongs to another stored session")
            for table in ("sessions", "messages", "cursors", "session_routes"):
                db.execute(f"UPDATE {table} SET session_id=? WHERE session_id=?", (new, old))
            changes[old] = new
    return changes
