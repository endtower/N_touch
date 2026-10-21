"""验收单 EXE 首次安装及源码 Core：独立端口/数据，禁用所有 Truck 和外部平台。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import uuid
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True)
    args = parser.parse_args()
    release = args.release.resolve()
    fixture = ROOT / "tmp" / ("public-source-install-" + uuid.uuid4().hex)
    program, data = fixture / "program", fixture / "data"
    (data / "manager").mkdir(parents=True)
    manager_port, ingress_port, web_port, main_port = [free_port() for _ in range(4)]
    (data / ".env.dev").write_text(f"ONEBOT_ENABLED=false\nBRIDGE_ENABLED=false\nINGRESS_API_PORT={ingress_port}\nWEB_WS_PORT={web_port}\nMAIN_PORT={main_port}\n", encoding="utf-8")
    (data / "manager/update_sources.json").write_text(json.dumps({"auto_check": False, "sources": {"snowluma": ""}}), encoding="utf-8")
    (data / "manager/trucks.json").write_text(json.dumps({"startup_paused": True}), encoding="utf-8")
    (data / "sentinel.txt").write_text("keep-user-data", encoding="utf-8")
    env = os.environ.copy()
    for key in list(env):
        if key.startswith(("FLASH_", "OPENCODE_", "VECTOR_", "PERSONA_", "CONVERSATION_", "VISION_", "LLM_")):
            env.pop(key, None)
    env.pop("ABYSS_RUN_MODE", None)
    env.update({"ABYSS_DATA_DIR": str(data), "ABYSS_INSTALL_DIR": str(program), "ABYSS_MANAGER_PORT": str(manager_port),
                "ABYSS_LAUNCHER_MUTEX": "PublicSourceAcceptance_" + uuid.uuid4().hex,
                "ABYSS_UPDATES_SNAPSHOT": str(fixture / "first-install.png"), "ABYSS_UPDATES_EXIT_AFTER_SNAPSHOT": "1"})
    env["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
    startup = subprocess.STARTUPINFO(); startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW; startup.wShowWindow = subprocess.SW_HIDE
    launcher = subprocess.Popen([str(release / "download/AbyssLauncher.exe")], cwd=fixture, env=env, startupinfo=startup)
    base = f"http://127.0.0.1:{manager_port}"

    def call(path, body=None, allow_error=False):
        token = (data / "manager/service_token.txt").read_text(encoding="utf-8").strip()
        request = Request(base + path, headers={"X-Manager-Token": token, "Content-Type": "application/json"},
                          data=json.dumps(body).encode() if body is not None else None)
        try:
            with urlopen(request, timeout=180) as response:
                return json.load(response)
        except HTTPError as error:
            result = json.load(error)
            if allow_error:
                return result
            raise AssertionError(f"{path}: {result}; fixture={fixture}") from error

    def until(predicate, timeout=180):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                value = predicate()
                if value:
                    return value
            except Exception as error:
                last = error
            time.sleep(0.4)
        raise AssertionError(f"Timed out: {last}; fixture={fixture}")

    def targets():
        return {item["name"]: item for item in call("/v1/targets")["targets"]}

    def confirm_install(version):
        review = call("/v1/updates/review", {"component": "core", "operation": "install", "version": version})
        assert review["can_proceed"], review
        return call("/v1/updates/apply", {"component": "core", "version": version, "confirmation_token": review["confirmation_token"]})

    try:
        info = until(lambda: call("/v1/info"), 100)
        assert info["version"] == "0.9.0", info
        assert (program / "manager.exe").exists() and (program / "updater/AbyssUpdater.exe").exists()
        assert (program / "AbyssLauncher.exe").exists(), "Download entry did not install its updatable launcher"
        status = call("/v1/updates")
        assert next(x for x in status["components"] if x["component"] == "core")["current"] == "未安装"
        assert not (program / "components/core").exists()
        until(lambda: (fixture / "first-install.png").exists(), 60)
        launcher.wait(20)
        print("PASS single EXE bootstraps manager without system Python or automatic Core download", flush=True)
        version = json.loads((release / "release-summary.json").read_text())["versions"]["core"]
        archive = release / "packages" / f"core-{version}.zip"
        call("/v1/updates/import", {"component": "core", "path": str(archive)})
        denied = call("/v1/updates/apply", {"component": "core", "version": version}, allow_error=True)
        assert denied.get("ok") is False and "确认" in denied["message"], denied
        assert not call("/v1/updates")["active"]
        confirm_install(version)
        until(lambda: not call("/v1/updates")["active"], 600)
        result = next(x for x in call("/v1/updates")["components"] if x["component"] == "core")["result"]
        assert result["state"] == "installed", result
        bundle = program / "components/core"
        assert (bundle / "source/web_frontend/dist/index.html").exists()
        assert (bundle / "runtime/python.exe").exists()
        print("PASS confirmed source Core installation includes Python runtime and matching UI", flush=True)
        started = call("/v1/targets/core/start", {})
        assert started["ok"], started
        current = targets()
        assert current["core"]["ready"] and current["ingress"]["ready"], current
        # 核对真正启动的是组件中的解释器，不能偷偷退回本机 Python。
        import psutil
        assert Path(psutil.Process(current["core"]["pid"]).exe()).resolve() == (bundle / "runtime/python.exe").resolve()
        assert Path(psutil.Process(current["ingress"]["pid"]).exe()).resolve() == (bundle / "runtime/python.exe").resolve()
        with urlopen(Request(base + "/console/trucks", headers={"X-Manager-Token": (data / "manager/service_token.txt").read_text().strip()}), timeout=20) as response:
            assert all(x["runtime_state"] == "stopped" for x in json.load(response)["trucks"])
        print("PASS isolated Core and ingress start from bundled source with all Trucks disabled", flush=True)
        source = bundle / "source/brain/att/proactive/proactive_core.py"
        original = source.read_bytes()
        source.write_bytes(original + b"\n# local user edit\n")
        # 下载已缓存版本仍需单独确认。
        denied = call("/v1/updates/download", {"component": "core", "version": version}, allow_error=True)
        assert denied.get("ok") is False and "确认" in denied["message"], denied
        review = call("/v1/updates/review", {"component": "core", "operation": "download", "version": version})
        call("/v1/updates/download", {"component": "core", "version": version, "confirmation_token": review["confirmation_token"]})
        review = call("/v1/updates/review", {"component": "core", "operation": "install", "version": version})
        assert not review["can_proceed"] and review["local_changes"], review
        assert source.read_bytes().endswith(b"# local user edit\n")
        print("PASS local modifications block replacement; download does not imply installation", flush=True)
        source.write_bytes(original)
        previous = targets()
        confirm_install(version)
        until(lambda: not call("/v1/updates")["active"], 600)
        current = targets()
        assert current["core"]["ready"] and current["ingress"]["ready"], current
        assert current["core"]["pid"] != previous["core"]["pid"]
        assert (data / "sentinel.txt").read_text() == "keep-user-data"
        backups = list((data / "manager/updates/jobs").glob("*/data-backup/memory/memory.db"))
        assert backups, "Core data backup missing"
        print("PASS confirmed reinstall backs up data and restores previously running source processes", flush=True)
        # 验证外部更新器真实返回码；恢复成功不能被管理服务误判成失败。
        target, payload, transaction = (fixture / name for name in ("rollback-program", "rollback-payload", "rollback-transaction"))
        for folder in (target, payload, transaction, fixture / "rollback-data/manager/updates"):
            folder.mkdir(parents=True)
        (target / "core.exe").write_bytes(b"old")
        (payload / "core.exe").write_bytes(b"new")
        job = {"id": "cli-rollback", "component": "core", "version": version,
               "target_root": str(target), "app_root": str(target), "data_root": str(fixture / "rollback-data"),
               "payload": str(payload), "receipt": str(target / ".versions/core.json"),
               "result": str(transaction / "result.json"), "previous_files": ["core.exe"],
               "files": {"core.exe": hashlib.sha256(b"new").hexdigest()}}
        job_path = transaction / "job.json"
        job_path.write_text(json.dumps(job), encoding="utf-8")
        helper = release / "updater/AbyssUpdater.exe"
        for flags in (("--files-only",), ("--rollback", "--files-only")):
            subprocess.run([str(helper), str(job_path), *flags], check=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        assert (target / "core.exe").read_bytes() == b"old"
        print("PASS external updater reports successful program rollback correctly", flush=True)
        print(f"PASS public installation workflow; fixture={fixture}", flush=True)
    finally:
        try: call("/v1/shutdown", {})
        except Exception: pass
        if launcher.poll() is None:
            launcher.terminate()
            launcher.wait(10)


if __name__ == "__main__":
    main()
