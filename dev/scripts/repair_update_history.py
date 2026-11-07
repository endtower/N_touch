"""Restore archived version catalogs and rebind backup receipts after relocation.

Only small management metadata is changed. Package bytes, program backups, file
hash baselines and user databases are never rewritten. Safe to run repeatedly.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import uuid
from datetime import datetime

from workspace_layout import WORKSPACE


def encode(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def atomic(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".repair.tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def repair(workspace=WORKSPACE):
    workspace = Path(workspace).resolve()
    state = workspace / "data/manager/updates"
    legacy = workspace / "backups/legacy-updates"
    if (state / "active.json").exists():
        raise ValueError("已有更新任务正在进行，完成或恢复任务后再修复版本记录")
    changes = {}
    restored_catalogs = 0
    rebound_receipts = 0
    for archived in (legacy / "catalog").glob("*.json"):
        original = json.loads(archived.read_text(encoding="utf-8-sig"))
        target = state / "catalog" / archived.name
        if target.is_file():
            current = json.loads(target.read_text(encoding="utf-8-sig"))
            if current.get("source") != original.get("source"):
                continue  # An intentional feed change must not restore another feed.
            releases = {v["version"]: v for v in original.get("releases", [])}
            releases.update({v["version"]: v for v in current.get("releases", [])})
            value = {**current, "releases": list(releases.values())}
            if value == current:
                continue
        else:
            value = original
        changes[target] = encode(value)
        restored_catalogs += 1

    old_apps = {workspace / "常用版本", workspace / "dist/Abyss"}
    old_snow = {workspace / "SnowLuma", workspace / "data/SnowLuma", *(p / "SnowLuma" for p in old_apps)}
    receipts = set()
    for directory in (legacy / "jobs", state / "jobs", workspace / "backups/updates"):
        receipts.update(directory.glob("*/previous-receipt.json"))
        receipts.update(directory.glob("*/*/previous-receipt.json"))
    for path in sorted(receipts):
        job_path, journal_path = path.parent / "job.json", path.parent / "journal.json"
        if not job_path.is_file() or not journal_path.is_file():
            continue
        job = json.loads(job_path.read_text(encoding="utf-8-sig"))
        journal = json.loads(journal_path.read_text(encoding="utf-8-sig"))
        if journal.get("state") not in ("installed", "rolled_back"):
            continue
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        recorded = value.get("target_root")
        if not recorded or not Path(recorded).is_absolute():
            continue
        component = job.get("component")
        previous = Path(recorded).resolve()
        if component == "snowluma" and previous in old_snow:
            destination = workspace / "external/snowluma"
        elif component in ("core", "manager", "ingress", "launcher") and previous in old_apps:
            destination = workspace / "app"
        else:
            continue
        value["target_root"] = str(destination)
        changes[path] = encode(value)
        rebound_receipts += 1

    if not changes:
        return {"ok": True, "catalogs_restored": 0, "backup_receipts_rebound": 0}
    snapshot = workspace / "backups/update-history-repair" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    before = {p: p.read_bytes() if p.is_file() else None for p in changes}
    records = []
    for path, content in changes.items():
        relative = path.relative_to(workspace)
        if before[path] is not None:
            saved = snapshot / "before" / relative
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_bytes(before[path])
        records.append({"path": relative.as_posix(), "existed": before[path] is not None,
                        "sha256_after": hashlib.sha256(content).hexdigest()})
    log = {"state": "prepared", "files": records}
    atomic(snapshot / "repair.json", encode(log))
    applied = []
    try:
        for path, content in changes.items():
            if (path.read_bytes() if path.is_file() else None) != before[path]:
                raise RuntimeError("版本记录正在变化，已停止修复以保留并发更新")
            atomic(path, content)
            applied.append(path)
        log["state"] = "complete"
        atomic(snapshot / "repair.json", encode(log))
    except BaseException:
        for path in reversed(applied):
            if path.read_bytes() != changes[path]:
                continue
            if before[path] is None:
                path.unlink()
            else:
                atomic(path, before[path])
        log["state"] = "rolled_back"
        atomic(snapshot / "repair.json", encode(log))
        raise
    return {"ok": True, "catalogs_restored": restored_catalogs,
            "backup_receipts_rebound": rebound_receipts, "backup": str(snapshot)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=WORKSPACE)
    print(json.dumps(repair(parser.parse_args().workspace), ensure_ascii=False))
