#!/usr/bin/env python3
"""Render inline version selection and downgrade controls with isolated data."""
import json
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
program = ROOT / "dist/independent-updates/Abyss"

for theme, component in (("dark", "core"), ("light", "core"), ("dark", "manager")):
    data = ROOT / "tmp" / ("updates-ui-" + theme + "-" + uuid.uuid4().hex)
    (data / "manager").mkdir(parents=True)
    (data / "launcher").mkdir()
    (data / "SnowLuma").mkdir()
    (data / "SnowLuma/package.json").write_text('{"version":"1.12.1"}', encoding="utf-8")
    (data / "manager/update_sources.json").write_text(json.dumps({"auto_check": False, "sources": {"core": "https://example.test/core.json", "snowluma": ""}}), encoding="utf-8")
    (data / "manager/updates/catalog").mkdir(parents=True)
    releases = [{"component": "core", "version": v, "url": f"https://example.test/core-{v}.zip", "sha256": "a" * 64, "size": 123}
                for v in ("0.2.0", "0.1.0", "0.0.9")]
    (data / "manager/updates/catalog/core.json").write_text(json.dumps({"source": "https://example.test/core.json", "releases": releases}), encoding="utf-8")
    (data / "manager/updates/core").mkdir()
    (data / "manager/updates/core/ready.json").write_text(json.dumps({"component": "core", "version": "0.0.9", "files": {}}), encoding="utf-8")
    (data / "launcher/ui.json").write_text(json.dumps({"theme": theme, "closeToTray": True}), encoding="utf-8")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
    snapshot = data / f"updates-{theme}-{component}.png"
    env = os.environ.copy()
    env.update({"ABYSS_DATA_DIR": str(data), "ABYSS_MANAGER_PORT": str(port),
                "ABYSS_LAUNCHER_MUTEX": "UpdatesUiTest_" + uuid.uuid4().hex,
                "ABYSS_LAUNCHER_SECTION": "updates", "ABYSS_UPDATES_SNAPSHOT": str(snapshot),
                "ABYSS_UPDATES_SNAPSHOT_COMPONENT": component,
                "ABYSS_UPDATES_SNAPSHOT_VERSION": "0.0.9" if component == "core" else "0.6.1",
                "ABYSS_UPDATES_EXIT_AFTER_SNAPSHOT": "1"})
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    process = subprocess.Popen([str(program / "AbyssLauncher.exe")], env=env, cwd=program, startupinfo=startup)
    try:
        deadline = time.monotonic() + 40
        while not snapshot.is_file() and time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(f"Launcher exited before rendering. Fixture: {data}")
            time.sleep(0.3)
        assert snapshot.is_file(), f"Update center did not render; fixture: {data}"
        process.wait(10)
        assert process.returncode == 0
        print(f"PASS: {theme} inline {component} version selection rendered: {snapshot}", flush=True)
    finally:
        if process.poll() is None:
            process.terminate(); process.wait(5)
        token_path = data / "manager/service_token.txt"
        if token_path.is_file():
            try:
                request = Request(f"http://127.0.0.1:{port}/v1/shutdown", method="POST",
                                  headers={"X-Manager-Token": token_path.read_text().strip()})
                with urlopen(request, timeout=5): pass
            except Exception: pass
