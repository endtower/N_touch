"""Start the installed launcher and verify source service readiness locally."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from workspace_layout import test_root, CACHE, BUILDS
import subprocess
import time
from urllib.request import Request, urlopen

from workspace_layout import WORKSPACE as ROOT


def desktop_environment():
    env = os.environ.copy()
    # Match an ordinary desktop launch rather than inheriting a coding tool's
    # temporary proxy injection. Preserve proxies explicitly stored by the user.
    if os.name == "nt":
        import winreg
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
            value = None
            for hive, key in ((winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"), (winreg.HKEY_CURRENT_USER, "Environment")):
                try:
                    with winreg.OpenKey(hive, key) as handle:
                        value = winreg.QueryValueEx(handle, name)[0]
                except FileNotFoundError:
                    pass
            env.pop(name.lower(), None)
            if value is None:
                env.pop(name, None)
            else:
                env[name] = os.path.expandvars(str(value))
    return env


def main(start_core=False):
    app, data = ROOT / "app", ROOT / "data"
    if not (app / "components/manager/source/manager_service.py").is_file() or not data.is_dir():
        raise RuntimeError("源码式运行版尚未部署")
    env = desktop_environment()
    env["ABYSS_DATA_DIR"] = str(data)
    env["DOTNET_BUNDLE_EXTRACT_BASE_DIR"] = str(CACHE / "launcher-runtime")
    subprocess.Popen([str(app / "AbyssLauncher.exe")], cwd=app, env=env,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=(0x00000008 | 0x00000200) if os.name == "nt" else 0,
                     close_fds=True)

    def call(path, body=None):
        token = (data / "manager/service_token.txt").read_text(encoding="utf-8").strip()
        request = Request("http://127.0.0.1:49990" + path, headers={"X-Manager-Token":token, "Content-Type":"application/json"},
                          data=json.dumps(body).encode() if body is not None else None)
        with urlopen(request, timeout=120) as response:
            return json.load(response)

    deadline = time.monotonic() + 50
    info = None
    while time.monotonic() < deadline:
        try:
            info = call("/v1/info")
            break
        except OSError:
            time.sleep(0.5)
    if info is None or Path(info["instance"]) != data:
        raise RuntimeError("新管理服务未就绪或数据目录不匹配")
    if start_core:
        result = call("/v1/targets/core/start", {})
        if not result.get("ok"):
            raise RuntimeError("管理服务已启动，但 Core 未就绪：" + result.get("message", ""))
    storage = call("/v1/storage")
    targets = call("/v1/targets")["targets"]
    print(json.dumps({"manager_version":info["version"], "data":storage["data"], "app":storage["app"],
          "targets":[{"name":t["name"],"state":t["state"],"ready":t["ready"],"pid":t.get("pid")} for t in targets]}, ensure_ascii=False),flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-core", action="store_true")
    main(parser.parse_args().start_core)
