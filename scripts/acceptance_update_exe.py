#!/usr/bin/env python3
"""Exercise real frozen manager + C# updater using an isolated installed copy."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, default=ROOT / "dist/independent-updates")
    args = parser.parse_args()
    release = args.release.resolve()
    versions = json.loads((release / "Abyss/component_versions.json").read_text(encoding="utf-8"))
    test_root = ROOT / "tmp" / ("update-exe-" + uuid.uuid4().hex)
    program, data = test_root / "program", test_root / "data"
    shutil.copytree(release / "Abyss", program)
    data.mkdir()
    manager_port, ingress_port, onebot_port, web_port = [free_port() for _ in range(4)]
    (data / ".env.dev").write_text(f"INGRESS_API_PORT={ingress_port}\nONEBOT_WS_PORT={onebot_port}\nWEB_WS_PORT={web_port}\n", encoding="utf-8")
    (data / "sentinel.txt").write_text("user-data-must-remain", encoding="utf-8")
    (data / "manager").mkdir()
    (data / "manager/update_sources.json").write_text(json.dumps({"auto_check": False, "sources": {"snowluma": ""}}), encoding="utf-8")
    env = os.environ.copy()
    env["ABYSS_DATA_DIR"] = str(data)
    env.pop("ABYSS_RUN_MODE", None)
    sys.path.insert(0, str(ROOT))
    from config_keys import CONFIG_OVERRIDE_KEYS
    for key in CONFIG_OVERRIDE_KEYS:
        env.pop(key, None)
    manager = subprocess.Popen([str(program / "manager.exe"), "--port", str(manager_port)], cwd=program, env=env,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    base = f"http://127.0.0.1:{manager_port}"

    def call(path, body=None):
        if path in ("/v1/updates/download", "/v1/updates/apply") and body and body.get("component") == "core":
            review = call("/v1/updates/review", {"component": "core", "operation": "download" if path.endswith("download") else "install", "version": body.get("version")})
            assert review["can_proceed"], review
            body = {**body, "confirmation_token": review["confirmation_token"]}
        token = (data / "manager/service_token.txt").read_text().strip()
        request = Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                          headers={"X-Manager-Token": token, "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=100) as response:
                return json.load(response)
        except HTTPError as error:
            raise AssertionError(f"{path}: {error.read().decode('utf-8')}; fixture: {test_root}") from error

    def until(predicate, timeout=90):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                value = predicate()
                if value: return value
            except Exception as error:
                last = error
            time.sleep(0.3)
        raise AssertionError(f"Timed out: {last}; fixture retained at {test_root}")

    try:
        info = until(lambda: call("/v1/info"), 35)
        assert info["version"] == versions["manager"], info
        assert call("/v1/targets/ingress/start", {})["ok"]
        targets = {item["name"]: item for item in call("/v1/targets")["targets"]}
        ingress_pid = targets["ingress"]["pid"]
        assert targets["ingress"]["ready"]
        core_package = release / "packages" / f"core-{versions['core']}.zip"
        call("/v1/updates/import", {"component": "core", "path": str(core_package)})
        call("/v1/updates/apply", {"component": "core"})
        until(lambda: not call("/v1/updates")["active"])
        receipt = json.loads((program / ".versions/core.json").read_text())
        assert receipt["version"] == versions["core"]
        targets = {item["name"]: item for item in call("/v1/targets")["targets"]}
        assert targets["ingress"]["pid"] == ingress_pid and targets["ingress"]["ready"], targets
        print("PASS: real Core installation leaves running ingress untouched", flush=True)

        # Use the same tested executable with a higher package version to exercise
        # the real installer transaction in both version directions, without LLM calls.
        sys.path.insert(0, str(ROOT / "scripts"))
        from package_component import package
        major, minor, _ = map(int, versions["core"].split("."))
        newer_version = f"{major}.{minor + 1}.0"
        newer = package("core", newer_version, release / "components/core", test_root / "packages")
        call("/v1/updates/import", {"component": "core", "path": str(newer)})
        call("/v1/updates/apply", {"component": "core", "version": newer_version})
        until(lambda: not call("/v1/updates")["active"])
        assert json.loads((program / ".versions/core.json").read_text())["version"] == newer_version
        row = next(r for r in call("/v1/updates")["components"] if r["component"] == "core")
        assert {newer_version, versions["core"]}.issubset({v["version"] for v in row["versions"]})
        call("/v1/updates/download", {"component": "core", "version": versions["core"]})
        call("/v1/updates/apply", {"component": "core", "version": versions["core"]})
        until(lambda: not call("/v1/updates")["active"])
        assert json.loads((program / ".versions/core.json").read_text())["version"] == versions["core"]
        assert next(t for t in call("/v1/targets")["targets"] if t["name"] == "ingress")["pid"] == ingress_pid
        assert (data / "sentinel.txt").read_text() == "user-data-must-remain"
        print("PASS: real version selection, upgrade and offline downgrade preserve ingress and user data", flush=True)

        # Updating the controller itself must restore its previously running targets.
        call("/v1/updates/import", {"component": "manager", "path": str(release / "packages" / f"manager-{versions['manager']}.zip")})
        call("/v1/updates/apply", {"component": "manager"})
        until(lambda: not call("/v1/updates")["active"])
        targets = until(lambda: {item["name"]: item for item in call("/v1/targets")["targets"]})
        until(lambda: next(item for item in call("/v1/targets")["targets"] if item["name"] == "ingress")["ready"])
        assert (data / "sentinel.txt").read_text() == "user-data-must-remain"
        assert json.loads((program / ".versions/manager.json").read_text())["version"] == versions["manager"]
        print("PASS: real manager self-update restarts previously running ingress and preserves user data", flush=True)
        call("/v1/shutdown", {})
        print(f"PASS: frozen update workflow. Fixture: {test_root}", flush=True)
    finally:
        try: call("/v1/shutdown", {})
        except Exception: pass
        try: manager.wait(8)
        except subprocess.TimeoutExpired: manager.terminate()


if __name__ == "__main__":
    main()
