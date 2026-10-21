"""Read-only inventory before platform-prefix migration. Never prints contact IDs or text.

No apply mode is provided. SQLite connections use mode=ro and query_only.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def reference_count(value, legacy: set[str]) -> int:
    if isinstance(value, str):
        return int(value in legacy)
    if isinstance(value, list):
        return sum(reference_count(item, legacy) for item in value)
    if isinstance(value, dict):
        return sum(int(key in legacy) + reference_count(item, legacy) for key, item in value.items())
    return 0


def inspect(data_dir: Path) -> dict:
    result = {"read_only": True, "migration_applied": False, "history_present": False,
              "session_formats": {}, "rename_candidates": 0, "ambiguous_legacy_sessions": 0,
              "destination_collisions": 0, "messages_affected": 0, "cursors_affected": 0,
              "structured_database_references": {}, "json_snapshot_references": 0,
              "json_files_with_references": 0, "json_files_skipped": 0}
    legacy = set()
    history = data_dir / "memory/chat_history/chat_history.db"
    if history.is_file():
        result["history_present"] = True
        with closing(sqlite3.connect(history.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            ids = {row[0] for row in db.execute("SELECT session_id FROM sessions")}
            formats = Counter()
            for sid in ids:
                if re.fullmatch(r"(?:private|group)_[0-9]+", sid):
                    legacy.add(sid)
                    formats["legacy_numeric_qq"] += 1
                elif sid.startswith(("qq_", "wechat_", "web_")):
                    formats[sid.partition("_")[0] + "_prefixed"] += 1
                else:
                    formats["legacy_needs_source_confirmation"] += 1
            result["session_formats"] = dict(formats)
            result["rename_candidates"] = len(legacy)
            result["ambiguous_legacy_sessions"] = formats["legacy_needs_source_confirmation"]
            result["destination_collisions"] = sum("qq_" + sid in ids for sid in legacy)
            for table, label in (("messages", "messages_affected"), ("cursors", "cursors_affected")):
                result[label] = sum(row[1] for row in db.execute(
                    f"SELECT session_id,COUNT(*) FROM {identifier(table)} GROUP BY session_id") if row[0] in legacy)
    memory_db = data_dir / "memory/memory.db"
    if memory_db.is_file() and legacy:
        with closing(sqlite3.connect(memory_db.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            for table in tables:
                for col in db.execute(f"PRAGMA table_info({identifier(table)})").fetchall():
                    name = col[1]
                    if "session" not in name and name not in ("contacts", "details_json", "issues_json"):
                        continue
                    count = 0
                    for (value,) in db.execute(f"SELECT {identifier(name)} FROM {identifier(table)}"):
                        if not isinstance(value, str):
                            continue
                        try:
                            structured = json.loads(value)
                        except (ValueError, TypeError):
                            structured = value
                        count += reference_count(structured, legacy)
                    if count:
                        result["structured_database_references"][f"{table}.{name}"] = count
    for directory in ("event_history", "pfca", "social_agent"):
        root = data_dir / "memory" / directory
        if not root.is_dir():
            continue
        for path in root.rglob("*.json"):
            if path.stat().st_size > 2_000_000:
                result["json_files_skipped"] += 1
                continue
            try:
                count = reference_count(json.loads(path.read_text(encoding="utf-8-sig")), legacy)
            except (OSError, ValueError):
                result["json_files_skipped"] += 1
                continue
            result["json_snapshot_references"] += count
            result["json_files_with_references"] += int(count > 0)
    return result


if __name__ == "__main__":
    from paths import data_root
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = inspect((args.data_dir or data_root()).resolve())
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
