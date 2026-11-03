#!/usr/bin/env python3
"""Deploy tested components to the established 常用版本 (compat link dist/Abyss), preserving user data.

Uses the C# installer's backup and rollback transaction, including for the initial
bootstrap from releases whose manager did not yet support update APIs.
"""
from __future__ import annotations

import hashlib
import argparse
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import psutil

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))
from manager_updates import unpack, write_json
from update_policy import allowed_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--components", nargs="+", choices=("core", "ingress", "manager", "launcher"),
                        default=["core", "ingress", "manager", "launcher"])
    args = parser.parse_args()
    release = ROOT / "dist/independent-updates"
    program = ROOT / "常用版本"
    link = ROOT / "dist/Abyss"
    if not link.is_dir() or link.resolve() != program.resolve():
        raise ValueError("Deployment target must be the existing 常用版本 (linked as dist/Abyss)")
    # 进程路径比较使用真实目录；兼容链接只用于保留旧脚本入口。
    target = program
    folder = ROOT / "manager/updates/jobs" / ("bootstrap-" + uuid.uuid4().hex)
    folder.mkdir(parents=True)
    helper = folder / "AbyssUpdater.exe"
    shutil.copy2(release / "Abyss/updater/AbyssUpdater.exe", helper)
    versions = json.loads((ROOT / "component_versions.json").read_text(encoding="utf-8"))
    jobs = []
    for name in dict.fromkeys(args.components):
        archive = release / "packages" / f"{name}-{versions[name]}.zip"
        manifest = json.loads(archive.with_suffix(".json").read_text(encoding="utf-8"))
        with archive.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != manifest["sha256"] or archive.stat().st_size != manifest["size"]:
            raise ValueError(f"Release integrity failed: {name}")
        job_dir = folder / name
        files = unpack(archive, job_dir / "payload", name)
        owned = [p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file() and
                 allowed_file(name, p.relative_to(target).as_posix())]
        job = {"id": folder.name + "-" + name, "component": name, "version": versions[name],
               "files": files, "previous_files": owned, "target_root": str(target), "app_root": str(target),
               "data_root": str(ROOT), "payload": str(job_dir / "payload"),
               "receipt": str(target / ".versions" / f"{name}.json"), "result": str(job_dir / "result.json")}
        write_json(job_dir / "job.json", job)
        jobs.append(job_dir / "job.json")
        # The initial local deployment is also a reusable offline version choice.
        cache = ROOT / "manager/updates/packages" / name / versions[name]
        cache.mkdir(parents=True, exist_ok=True)
        shutil.copy2(archive, cache / "archive.tmp")
        (cache / "archive.tmp").replace(cache / "archive.zip")
        write_json(cache / "release.json", manifest)
    # No installed program is touched until every release package passes validation.
    base = "http://127.0.0.1:49990"
    token_path = ROOT / "manager/service_token.txt"

    def call(path, method="GET"):
        headers = {"X-Manager-Token": token_path.read_text().strip()} if token_path.is_file() else {}
        with urlopen(Request(base + path, method=method, headers=headers), timeout=100) as response:
            return json.load(response)

    info = None
    try: info = call("/v1/info")
    except HTTPError: raise RuntimeError("运行中的管理服务与本项目数据根不匹配，拒绝替换")
    except (URLError, TimeoutError): pass
    previous = []
    if info:
        if Path(info["instance"]).resolve() != ROOT:
            raise RuntimeError("管理服务使用另一个数据目录，拒绝替换")
        try:
            if call("/v1/updates").get("active"):
                raise RuntimeError("已有更新任务，请先完成或恢复该任务")
        except HTTPError as error:
            if error.code != 404: raise
        except json.JSONDecodeError:
            # Legacy 0.5 routes unknown APIs to the HTML dashboard with HTTP 200.
            if not str(info.get("version", "")).startswith("0.5."): raise
        previous = [t["name"] for t in call("/v1/targets")["targets"] if t.get("desired") == "running"]
        manager_pid = info.get("pid")
        if not manager_pid:
            owners = {c.pid for c in psutil.net_connections(kind="tcp")
                      if c.status == psutil.CONN_LISTEN and c.laddr.port == 49990 and c.pid}
            if len(owners) != 1: raise RuntimeError("无法确认旧管理服务进程，拒绝替换")
            manager_pid = owners.pop()
            process = psutil.Process(manager_pid)
            command = process.cmdline()
            if not (Path(process.exe()).resolve() == target / "manager.exe" or
                    any(Path(arg).name == "manager_service.py" for arg in command) and
                    Path(process.cwd()).resolve() == ROOT):
                raise RuntimeError("旧管理服务进程不属于本项目，拒绝替换")
        result = call("/v1/shutdown", "POST")
        if not result.get("ok"): raise RuntimeError(result.get("message", "无法停止管理服务"))
        deadline = time.monotonic() + 25
        while psutil.pid_exists(manager_pid) and time.monotonic() < deadline: time.sleep(0.2)
        if psutil.pid_exists(manager_pid): raise RuntimeError("管理服务尚未退出，拒绝替换文件")

    launchers = []
    for process in psutil.process_iter(["pid", "name", "exe"]):
        try:
            path = Path(process.info["exe"]).resolve() if process.info["exe"] else None
            if path == target / "AbyssLauncher.exe": launchers.append(process)
            elif path in (target / "core.exe", target / "manager.exe", target / "ingress.exe"):
                raise RuntimeError(f"{path.name} 仍在运行，拒绝替换")
        except (psutil.NoSuchProcess, psutil.AccessDenied): pass
    for launcher in launchers:
        # The old launcher has no command-line exit protocol. Its background targets
        # were stopped through the manager above; preferences are already persisted.
        launcher.terminate(); launcher.wait(10)
    try:
        for path in jobs:
            subprocess.run([str(helper), str(path), "--files-only"], check=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        # 只更新本次实际部署的组件版本；整份复制会把未部署组件（如 Core）
        # 的基线版本改成仓库里的新版本，导致界面版本与真实程序不一致。
        deployed = json.loads((target / "component_versions.json").read_text(encoding="utf-8")) \
            if (target / "component_versions.json").is_file() else {}
        deployed.update({name: versions[name] for name in args.components})
        write_json(target / "component_versions.json", deployed)
        print(f"Deployed: {target}\nProgram backups: {folder}", flush=True)
    finally:
        if info or launchers:
            env = os.environ.copy(); env["ABYSS_DATA_DIR"] = str(ROOT)
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = subprocess.SW_HIDE
            subprocess.Popen([str(target / "AbyssLauncher.exe")], cwd=ROOT, env=env, startupinfo=startup)
            if previous:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    try: call("/v1/info"); break
                    except (URLError, TimeoutError): time.sleep(0.5)
                for name in sorted(previous, key=lambda n: {"ingress": 0, "snowluma": 1, "core": 2}.get(n, 3)):
                    result = call(f"/v1/targets/{name}/start", "POST")
                    if not result.get("ok"): print(f"Installed; {name} could not restart: {result.get('message')}")


if __name__ == "__main__":
    main()
