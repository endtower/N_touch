"""Back up and migrate the local workspace to a source-only running layout.

Defaults to a preview. --execute performs the already-reviewed plan. No data
contents are printed, no database schema/content transformations are performed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time

import psutil

from workspace_layout import SOURCE, WORKSPACE as ROOT
sys.path.insert(0, str(SOURCE))
from core_update_review import installed_files


def inside(path):
    resolved = path.resolve()
    if not resolved.is_relative_to(ROOT) or resolved == ROOT:
        raise ValueError("迁移路径必须位于当前工作区内")
    return path


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def assert_stopped():
    for process in psutil.process_iter(["exe", "cmdline"]):
        try:
            exe = Path(process.info["exe"]).resolve() if process.info["exe"] else None
            scripts = process.info["cmdline"] or []
            if exe and exe.parent in (ROOT / "app", ROOT / "常用版本") and exe.name.lower() in ("abysslauncher.exe", "core.exe", "manager.exe", "ingress.exe"):
                raise ValueError("日常运行版仍在运行，未开始迁移")
            for arg in scripts:
                if Path(arg).name in ("manager_service.py", "main.py", "ingress_service.py") and str(ROOT).lower() in arg.lower():
                    raise ValueError("工作区内还有数据写入进程，未开始迁移")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if any((ROOT / name).exists() for name in ("manager/updates/active.json", "data/manager/updates/active.json")):
        raise ValueError("旧更新任务尚未结束，拒绝移动任务资料")


def files_under(directory):
    if not directory.exists():
        return
    if directory.is_symlink() or directory.is_junction():
        raise ValueError("迁移目录包含链接")
    for parent, directories, filenames in os.walk(directory, followlinks=False):
        directories[:] = [n for n in directories if n != "__pycache__"]
        for child in [*(Path(parent) / n for n in directories), *(Path(parent) / n for n in filenames)]:
            if child.is_symlink() or child.is_junction():
                raise ValueError("迁移目录包含链接")
        for name in filenames:
            path = Path(parent) / name
            if path.suffix not in (".py", ".pyc"):
                yield path


def plan():
    result = {}
    for name in ("memory", "user_settings", "logs", "launcher/webview", "brain/amy/persistence", "brain/hc/shortlight/notes", "adapt/web/data"):
        for p in files_under(ROOT / name):
            result[p] = ROOT / "data" / p.relative_to(ROOT)
    persona = ROOT / "brain/base/persona"
    for p in persona.glob("*.json"):
        result[p] = ROOT / "data" / p.relative_to(ROOT)
    for p in files_under(persona / "history"):
        result[p] = ROOT / "data" / p.relative_to(ROOT)
    for p in files_under(ROOT / "manager"):
        relative = p.relative_to(ROOT)
        if p.is_relative_to(ROOT / "manager/updates/catalog"):
            # Version catalogs are live management state, not archived payloads.
            result[p] = ROOT / "data" / relative
        else:
            result[p] = ROOT / "backups/legacy-updates" / p.relative_to(ROOT / "manager/updates") if p.is_relative_to(ROOT / "manager/updates") else ROOT / "data" / relative
    for name in (".env.dev", "launcher/ui.json", "launcher/connections.json"):
        if (ROOT / name).is_file(): result[ROOT / name] = ROOT / "data" / name
    for name in ("web_frontend/memory", "web_frontend/logs", "config/.env"):
        old = ROOT / name
        candidates = [old] if old.is_file() else files_under(old)
        for p in candidates:
            result[p] = ROOT / "data/legacy" / p.relative_to(ROOT)
    for source, target in result.items():
        inside(source); inside(target)
        if target.exists():
            raise ValueError("目标已有同名文件，拒绝覆盖；请检查迁移是否已执行")
    return sorted(result.items(), key=lambda item: item[0].as_posix())


def deploy(program_source: Path, execute=False):
    program_source = inside(program_source.resolve())
    target = inside(ROOT / "app")
    if not (program_source / "AbyssLauncher.exe").is_file() or not (program_source / "components/manager/source/manager_service.py").is_file():
        raise ValueError("新的源码式运行目录不完整")
    for name in ("manager.exe", "core.exe", "ingress.exe", "updater/AbyssUpdater.exe"):
        if (program_source / name).exists():
            raise ValueError("新运行目录仍包含旧业务 EXE")
    assert_stopped()
    entries = plan()
    size = sum(source.stat().st_size for source, _ in entries)
    print(json.dumps({"files": len(entries), "GiB": round(size / 2**30, 2), "program": str(target), "execute": execute}, ensure_ascii=False), flush=True)
    if not execute:
        return
    if shutil.disk_usage(ROOT).free < size + 2 * 2**30:
        raise ValueError("磁盘空间不足以保留完整迁移前备份")
    migration = inside(ROOT / "backups/migrations" / time.strftime("%Y%m%d-%H%M%S"))
    migration.mkdir(parents=True)
    journal = {"state": "backing-up", "root": str(ROOT), "data": str(ROOT / "data"), "program": str(target), "files": []}
    journal_path = migration / "migration.json"
    write(journal_path, journal)
    for index, (source, destination) in enumerate(entries):
        relative = source.relative_to(ROOT).as_posix()
        saved = migration / "data-before" / relative
        saved.parent.mkdir(parents=True, exist_ok=True)
        before = digest(source)
        shutil.copy2(source, saved)
        if digest(saved) != before or digest(source) != before:
            raise ValueError("备份期间文件变化，未移动数据；迁移前副本已保留")
        journal["files"].append({"source": relative, "target": destination.relative_to(ROOT).as_posix(), "sha256": before})
        if index % 2000 == 0:
            write(journal_path, journal)
            print("Backed up", index + 1, "/", len(entries), flush=True)
    assert_stopped()
    journal["state"] = "backed-up"
    write(journal_path, journal)
    moved = []
    old_program = inside(migration / "program-before")
    installed = False
    try:
        for record in journal["files"]:
            source, destination = inside(ROOT / record["source"]), inside(ROOT / record["target"])
            if digest(source) != record["sha256"]:
                raise ValueError("备份后文件发生变化，停止迁移")
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.rename(destination)
            moved.append((source, destination))
        if target.exists():
            inside(target); inside(old_program)
            target.rename(old_program)
        inside(program_source); inside(target)
        program_source.rename(target)
        installed = True
        (target / "abyss-data-dir.txt").write_text("../data", encoding="utf-8")
        (target / "abyss-storage-dir.txt").write_text("..", encoding="utf-8")
        registrations = ROOT / "data/manager/installations.json"
        if not registrations.exists():
            legacy_snow = ROOT / "SnowLuma"
            snow = legacy_snow if legacy_snow.is_dir() else ROOT / "external/snowluma"
            write(registrations, {"snowluma": {"path": str(snow), "data_policy": "upstream-managed"}})
        versions = json.loads((target / "component_versions.json").read_text(encoding="utf-8"))
        for component in ("core", "manager"):
            write(target / ".versions" / f"{component}.json", {"version": versions[component], "target_root": str(target),
                  "files": installed_files(target, component), "baseline_kind": "local-source-migration"})
        launcher_files = {"AbyssLauncher.exe": digest(target / "AbyssLauncher.exe")}
        write(target / ".versions/launcher.json", {"version": versions["launcher"], "target_root": str(target), "files": launcher_files})
        for record in journal["files"]:
            if digest(ROOT / record["target"]) != record["sha256"]:
                raise ValueError("迁移后校验失败，开始恢复原布局")
        journal["state"] = "complete"
        journal["moved_count"] = len(moved)
        write(journal_path, journal)
        print("Migration complete; verified",len(moved),"files; backup:",migration,flush=True)
    except BaseException:
        if installed and target.exists():
            target.rename(program_source)
        if old_program.exists() and not target.exists():
            old_program.rename(target)
        for source, destination in reversed(moved):
            if destination.exists() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                destination.rename(source)
        journal["state"] = "rolled-back"
        write(journal_path, journal)
        raise


def migrate_launcher_state(migration):
    """Complete older migration plans that did not enumerate the WebView profile."""
    assert_stopped()
    migration = inside(migration.resolve())
    journal_path = migration / "migration.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    candidates = list(files_under(ROOT / "launcher/webview"))
    candidates += [ROOT / name for name in ("launcher/ui.json", "launcher/connections.json") if (ROOT / name).is_file()]
    records = []
    for source in candidates:
        relative = source.relative_to(ROOT)
        target = inside(ROOT / "data" / relative)
        if target.exists():
            raise ValueError("启动器资料目标已存在，未覆盖")
        saved = migration / "data-before" / relative
        saved.parent.mkdir(parents=True, exist_ok=True)
        checksum = digest(source)
        shutil.copy2(source, saved)
        if digest(saved) != checksum or digest(source) != checksum:
            raise ValueError("启动器资料备份期间发生变化")
        records.append({"source":relative.as_posix(), "target":target.relative_to(ROOT).as_posix(), "sha256":checksum})
    journal["state"] = "launcher-state-backed-up"
    journal["files"].extend(records)
    write(journal_path, journal)
    moved = []
    try:
        for record in records:
            source, target = inside(ROOT / record["source"]), inside(ROOT / record["target"])
            target.parent.mkdir(parents=True, exist_ok=True)
            source.rename(target)
            moved.append((source, target))
            if digest(target) != record["sha256"]:
                raise ValueError("启动器资料迁移校验失败")
        # The tracked file is a public template; personal entries now live in data.
        write(ROOT / "launcher/connections.json", {"version":1,"connections":[{"id":"snowluma","name":"SnowLuma","managedTarget":"snowluma"}]})
        journal["state"] = "complete"
        journal["moved_count"] = len(journal["files"])
        write(journal_path, journal)
        print("Launcher profile migrated and verified:",len(records),"files",flush=True)
    except BaseException:
        for source, target in reversed(moved):
            if target.exists() and not source.exists():
                target.rename(source)
        journal["state"] = "launcher-state-rolled-back"
        write(journal_path, journal)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--program-source", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    deploy(args.program_source, args.execute)
