"""Isolated source-manager/Core startup and single-launcher update acceptance."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from workspace_layout import test_root, CACHE, BUILDS
import socket
import subprocess
import tempfile
import time
from urllib.request import Request, urlopen

import psutil

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main(app: Path):
    app = app.resolve()
    with tempfile.TemporaryDirectory(prefix="source-layout-", dir=test_root()) as temporary:
        fixture = Path(temporary)
        data = fixture / "data"
        (data / "manager/updates").mkdir(parents=True)
        ports = {key: free_port() for key in ("MAIN_PORT", "WEB_WS_PORT", "INGRESS_API_PORT", "ONEBOT_WS_PORT")}
        manager_port = free_port()
        (data / ".env.dev").write_text("ONEBOT_ENABLED=false\nBRIDGE_ENABLED=false\nMASTER_ID=1\nASSISTANT_QQ=2\nFLASH_API_KEY=isolated-not-a-real-key\n" + "\n".join(f"{key}={value}" for key, value in ports.items()), encoding="utf-8")
        (data / "manager/trucks.json").write_text('{"startup_paused":true}', encoding="utf-8")
        (data / "manager/update_sources.json").write_text('{"auto_check":false,"sources":{"snowluma":""}}', encoding="utf-8")
        env = os.environ.copy()
        for key in list(env):
            if key.startswith(("FLASH_", "VECTOR_", "PERSONA_", "CONVERSATION_", "VISION_", "LLM_", "BRIDGE_")):
                env.pop(key, None)
        env["ABYSS_DATA_DIR"] = str(data)
        env.pop("ABYSS_RUN_MODE", None)
        env["DOTNET_BUNDLE_EXTRACT_BASE_DIR"] = str(CACHE / "launcher-runtime")
        # This private local build retains the user's initializer. Seed synthetic
        # memory so its empty-database embedding initialization never runs here.
        seed = """import asyncio
from brain.hc.database.core import get_memory_db
async def main():
    db = get_memory_db()
    connection = await db._ensure_connection()
    await connection.execute("INSERT INTO bequests (bequest_id,title) VALUES ('isolated','synthetic startup fixture')")
    await connection.commit()
    await connection.close()
asyncio.run(main())
"""
        subprocess.run([str(app / "components/core/runtime/python.exe"), "-c", seed], env=env, cwd=data, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        personas = data / "brain/base/persona"
        personas.mkdir(parents=True, exist_ok=True)
        for name in ("common", "normal"):
            (personas / f"{name}.json").write_text(json.dumps({"persona_name":name,"scene":"synthetic","kernel_prompt":"synthetic fixture","model_level":"persona"}), encoding="utf-8")
        log_path = fixture / "manager.log"
        output = log_path.open("wb")
        manager = subprocess.Popen([str(app / "components/manager/runtime/python.exe"), "-u", str(app / "components/manager/source/manager_service.py"), "--port", str(manager_port)],
                                   cwd=data, env=env, stdout=output, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        base = f"http://127.0.0.1:{manager_port}"

        def call(path, body=None):
            token = (data / "manager/service_token.txt").read_text(encoding="utf-8").strip()
            request = Request(base + path, headers={"X-Manager-Token": token, "Content-Type": "application/json"},
                              data=json.dumps(body).encode() if body is not None else None)
            with urlopen(request, timeout=120) as response:
                return json.load(response)

        def wait_info():
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if manager.poll() is not None:
                    raise AssertionError("Source manager exited before readiness")
                try:
                    return call("/v1/info")
                except OSError:
                    time.sleep(0.3)
            raise AssertionError("Source manager did not become ready")

        try:
            info = wait_info()
            assert not info["config_errors"], info["config_errors"]
            storage = call("/v1/storage")
            assert Path(storage["data"]) == data
            assert storage["snowluma"]["data_policy"] == "upstream-managed"
            started = call("/v1/targets/core/start", {})
            if not started["ok"]:
                import shutil
                shutil.copytree(data / "logs", test_root() / "source-layout-diagnostics", dirs_exist_ok=True)
                for path in (data / "logs/manager").glob("*.out.log"):
                    errors = [line for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
                              if line.startswith(("ModuleNotFoundError:", "ImportError:", "RuntimeError:", "NameError:", "TypeError:", "AttributeError:", "FileNotFoundError:", "openai.OpenAIError:"))]
                    print("Startup diagnostic", path.name, errors[-2:], flush=True)
            assert started["ok"], started
            targets = {t["name"]: t for t in call("/v1/targets")["targets"]}
            for name, entry in (("core", "main.py"), ("ingress", "ingress_service.py")):
                assert targets[name]["ready"], targets[name]
                process = psutil.Process(targets[name]["pid"])
                assert Path(process.exe()).resolve() == app / "components/core/runtime/python.exe"
                assert str(app / "components/core/source" / entry) in process.cmdline()
            print("PASS source manager, Core and ingress start from bundled interpreters; isolated data", flush=True)
        finally:
            try:
                call("/v1/shutdown", {})
                manager.wait(30)
            except Exception:
                for child in psutil.Process(manager.pid).children(recursive=True) if manager.poll() is None else []:
                    child.terminate()
                if manager.poll() is None:
                    manager.terminate(); manager.wait(10)
            output.close()

        target = fixture / "program"
        payload = fixture / "payload"
        relative = "components/core/source/main.py"
        (target / relative).parent.mkdir(parents=True)
        (payload / relative).parent.mkdir(parents=True)
        (target / relative).write_text("# old")
        (payload / relative).write_text("# new")
        import hashlib
        digest = hashlib.sha256((payload / relative).read_bytes()).hexdigest()
        job = {"id":"isolated-source", "component":"core", "version":"0.5.0", "app_root":str(target), "target_root":str(target), "data_root":str(data),
               "payload":str(payload), "files":{relative:digest}, "previous_files":[relative], "receipt":str(target / ".versions/core.json"), "result":str(fixture / "result.json")}
        job_path = fixture / "job.json"
        job_path.write_text(json.dumps(job), encoding="utf-8")
        result = subprocess.run([str(app / "AbyssLauncher.exe"), "--update-job", str(job_path), "--files-only"], cwd=fixture, env=env, timeout=90)
        assert result.returncode == 0, "Headless launcher update failed"
        assert (target / relative).read_text() == "# new"
        assert json.loads((fixture / "result.json").read_text())["state"] == "installed"
        print("PASS same launcher executable applies source update without a separate updater EXE", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    main(parser.parse_args().app)
