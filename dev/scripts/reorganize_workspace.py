"""Reorganize the local workspace, keeping a verified manifest and rollback journal.

Preview by default. Does not move data/, rewrite third-party configuration, or
reset source baselines. Run only with all workspace services stopped.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

import psutil

SOURCE = Path(__file__).resolve().parents[1]
WORKSPACE = SOURCE.parent if SOURCE.name == "dev" else SOURCE


def checked(path: Path) -> Path:
    path = path.absolute()
    if not path.resolve().is_relative_to(WORKSPACE.resolve()) or path.resolve() == WORKSPACE.resolve():
        raise ValueError(f"Path outside workspace: {path}")
    return path


def checksum(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def stopped():
    for process in psutil.process_iter(["pid", "exe", "cmdline"]):
        try:
            info = process.info
            exe = info["exe"] or ""
            args = info["cmdline"] or []
            owned = str(WORKSPACE).lower() in exe.lower()
            service = any(Path(arg).name in ("manager_service.py", "main.py", "ingress_service.py", "index.mjs")
                          and str(WORKSPACE).lower() in arg.lower() for arg in args)
            if (owned and Path(exe).name.lower() in ("abysslauncher.exe", "node.exe", "python.exe", "pythonw.exe")) or service:
                raise RuntimeError(f"Workspace program still running: PID {info['pid']}")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    active = WORKSPACE / "data/manager/updates/active.json"
    if active.exists():
        raise RuntimeError("An update is active; finish or recover it before reorganizing")


def files(directory: Path):
    if directory.is_file():
        yield directory
        return
    for parent, dirs, names in os.walk(directory, followlinks=False):
        for name in dirs + names:
            child = Path(parent) / name
            if child.is_symlink() or child.is_junction():
                raise ValueError(f"Unexpected link in relocation: {child}")
        for name in names:
            yield Path(parent) / name


def moves():
    result = []
    for name in ("adapt", "assets", "brain", "config", "core", "design", "docs", "launcher", "memory",
                 "release_defaults", "releases", "scripts", "tests", "updater", "updater-tests", "utils", "web_frontend"):
        result.append((WORKSPACE / name, WORKSPACE / "dev" / name))
    for path in WORKSPACE.iterdir():
        if path.is_file() and (path.suffix in (".py", ".toml") or path.name in
                              ("requirements.txt", "component_versions.json", ".env.example")):
            result.append((path, WORKSPACE / "dev" / path.name))
        elif path.is_file() and path.suffix == ".spec":
            result.append((path, WORKSPACE / "private/legacy-build-tools" / path.name))
    result.extend((WORKSPACE / old, WORKSPACE / new) for old, new in (
        ("常用版本", "app"), ("SnowLuma", "external/snowluma"),
        (".tools", "cache/tools"), ("tmp/koboldcpp", "private/experiments/koboldcpp"),
        ("tmp", "cache/legacy-tmp"), ("dist", "cache/builds/legacy-dist"),
        ("reference", "private/reference"), ("STORAGE.md", "dev/docs/STORAGE.md"),
        ("_tmp_ab_result.log", "cache/legacy-tmp/_tmp_ab_result.log"),
        (".versions/snowluma.json", "app/.versions/snowluma.json"),
    ))
    for name in ("logs", "manager", "user_settings", "NVIDIA Corporation", "reference-review-1022", "__pycache__", ".versions"):
        result.append((WORKSPACE / name, WORKSPACE / "cache/legacy-workspace" / name))
    return [(checked(a), checked(b)) for a, b in result if a.exists()]


def run(execute=False):
    if (WORKSPACE / "app").exists() or (WORKSPACE / "dev").exists():
        raise ValueError("Destination app/dev already exists; do not repeat a completed migration")
    stopped()
    entries = moves()
    for source, target in entries:
        if target.exists():
            raise FileExistsError(target)
    print(json.dumps({"workspace": str(WORKSPACE), "execute": execute,
                      "moves": [{"from": str(a.relative_to(WORKSPACE)), "to": str(b.relative_to(WORKSPACE))}
                                for a, b in entries]}, ensure_ascii=False), flush=True)
    if not execute:
        return
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = checked(WORKSPACE / "backups/workspace-layout" / stamp)
    backup.mkdir(parents=True)
    journal_path = backup / "migration.json"
    journal = {"state": "preparing", "workspace": str(WORKSPACE), "moves": [], "files": {}}
    write(journal_path, journal)
    for name in ("AGENTS.md", "README.md", ".gitignore", "启动Abyss.cmd", "data/manager/installations.json"):
        source = WORKSPACE / name
        if source.is_file():
            destination = backup / "files-before" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    (backup / "working-tree.patch").write_bytes(subprocess.check_output(["git", "diff", "--binary"], cwd=WORKSPACE))
    (backup / "git-status.txt").write_bytes(subprocess.check_output(["git", "status", "--short"], cwd=WORKSPACE))
    # Snapshot editable files before any follow-up path repairs. Dependencies and
    # build caches remain intact through same-volume renames and are not copied.
    for folder in (".github", "scripts", "launcher", "web_frontend", "config", "core", "docs"):
        for parent, dirs, names in os.walk(WORKSPACE / folder):
            dirs[:] = [n for n in dirs if n not in ("node_modules", "bin", "obj", "dist", "__pycache__")]
            for name in names:
                path = Path(parent) / name
                if path.suffix.lower() in (".py", ".cs", ".csproj", ".ts", ".json", ".md", ".yml", ".cmd"):
                    saved = backup / "files-before" / path.relative_to(WORKSPACE)
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, saved)
    for path in WORKSPACE.glob("*.py"):
        shutil.copy2(path, backup / "files-before" / path.name)
    # Runtime and external installation have complete pre-move copies. Account
    # data is opaque: no upstream file contents or data directories are changed.
    for name in ("常用版本", "SnowLuma"):
        shutil.copytree(WORKSPACE / name, backup / "programs-before" / name)
    link = WORKSPACE / "dist/Abyss"
    old_link_target = None
    if link.is_junction():
        old_link_target = link.resolve()
        if old_link_target != WORKSPACE / "常用版本":
            raise ValueError("Unexpected legacy launcher junction target")
        # rmdir removes this verified junction itself, never its contents.
        os.rmdir(checked(link))
        journal["removed_legacy_junction"] = str(old_link_target)
    moved = []
    try:
        for index, (source, target) in enumerate(entries):
            stopped()
            manifest = {p.relative_to(source).as_posix() if source.is_dir() else ".": checksum(p) for p in files(source)}
            if source.name in ("常用版本", "SnowLuma"):
                saved_root = backup / "programs-before" / source.name
                if any(checksum(saved_root / rel) != sha for rel, sha in manifest.items()):
                    raise RuntimeError("Pre-move program backup verification failed")
            target.parent.mkdir(parents=True, exist_ok=True)
            checked(source).rename(checked(target))
            moved.append((source, target))
            actual = {p.relative_to(target).as_posix() if target.is_dir() else ".": checksum(p) for p in files(target)}
            if actual != manifest:
                raise RuntimeError(f"Relocation verification failed: {source.name}")
            record = {"from": str(source.relative_to(WORKSPACE)), "to": str(target.relative_to(WORKSPACE)),
                      "files": len(manifest)}
            journal["moves"].append(record)
            journal["files"][record["to"]] = manifest
            journal["state"] = "moving"
            write(journal_path, journal)
            print(f"Verified relocation {index + 1}/{len(entries)}: {record['to']} ({len(manifest)} files)", flush=True)
        journal["state"] = "relocated-awaiting-path-repairs"
        write(journal_path, journal)
        print("Relocation backup:", backup, flush=True)
    except BaseException:
        for source, target in reversed(moved):
            if target.exists() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                checked(target).rename(checked(source))
        if old_link_target and not link.exists():
            subprocess.run(["powershell", "-NoProfile", "-Command",
                            "New-Item -ItemType Junction -Path $args[0] -Target $args[1] | Out-Null",
                            str(link), str(old_link_target)], check=True)
        journal["state"] = "rolled-back"
        write(journal_path, journal)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    run(parser.parse_args().execute)
