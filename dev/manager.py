#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""manager.py —— Abyss 管家 CLI（管理服务的客户端）

按任务书要求，CLI 不再自己持进程句柄，而是向常驻管理服务（manager.exe /
manager_service.py，127.0.0.1:49990）发命令。服务未运行时给出明确提示。

用法：
    python manager.py status
    python manager.py start|stop|restart <ingress|core|snowluma>
    python manager.py logs <core|core_out|ingress|snowluma|service> [lines]
    python manager.py config-get | config-set KEY=VALUE ...
    python manager.py service-start     # 拉起常驻管理服务（冻结包内）
    python manager.py shutdown          # 停止全部目标并退出服务
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from paths import app_root, data_root, ensure_cwd

app_root()
ensure_cwd()
sys.path.insert(0, str(app_root()))

BASE = "http://127.0.0.1:49990"
SERVICE_EXE = app_root() / "manager.exe"
SERVICE_PORT = 49990


def _token() -> str:
    try:
        return (data_root() / "manager" / "service_token.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _call(method: str, path: str, body: dict | None = None, timeout: float = 50):
    req = urllib.request.Request(BASE + path, method=method,
                                 headers={"X-Manager-Token": _token()})
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {"error_code": "http_error", "message": f"HTTP {e.code}"}
    except (urllib.error.URLError, OSError):
        return None, {"error_code": "service_down",
                      "message": "管理服务未运行（请先启动 AbyssLauncher，或 manager.py service-start）"}


def _service_running() -> bool:
    status, _ = _call("GET", "/v1/info", timeout=3)
    return status == 200


def _service_start_detached() -> bool:
    """拉起常驻管理服务（冻结包用 manager.exe；源码用 python manager_service.py）。"""
    import subprocess
    exe = app_root() / "manager.exe"
    source = app_root() / "components/manager/source/manager_service.py"
    if source.is_file():
        cmd = [str(app_root() / "components/manager/runtime/python.exe"), "-u", str(source)]
    elif exe.exists():
        cmd = [str(exe)]
    else:
        cmd = [sys.executable, str(app_root() / "manager_service.py")]
    try:
        subprocess.Popen(cmd, cwd=str(app_root()),
                         creationflags=0x00000008 | 0x00000200)   # DETACHED | NEW_GROUP
    except Exception as e:
        print(f"服务启动失败: {e}")
        return False
    import time
    for _ in range(20):
        if _service_running():
            return True
        time.sleep(0.5)
    print("服务已拉起但 10s 内未就绪")
    return False


def _print_result(result: dict) -> int:
    print(result.get("message", json.dumps(result, ensure_ascii=False)))
    return 0 if result.get("ok") else 1


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    cmd, rest = args[0].lower(), args[1:]

    if cmd == "service-start":
        if _service_running():
            print("管理服务已在运行")
            return 0
        return 0 if _service_start_detached() else 1

    if not _service_running():
        if not _service_start_detached():
            return 1

    if cmd == "status":
        code, body = _call("GET", "/v1/targets")
        if code is None:
            print(body["message"]); return 1
        for t in body.get("targets", []):
            print(f"{t['name']:<9} {t['state']:<12} 期望:{t['desired']:<9} "
                  f"pid:{t['pid'] or '-':<7} 端口:{','.join(map(str, t['ports']))} "
                  f"{'就绪' if t['ready'] else '未就绪'}")
        for err in body.get("config_errors", []):
            print(f"[配置警告] {err}")
        return 0
    if cmd in ("start", "stop", "restart"):
        if not rest:
            print(f"用法: {cmd} <ingress|core|snowluma>")
            return 2
        code, body = _call("POST", f"/v1/targets/{rest[0]}/{cmd}")
        return _print_result(body)
    if cmd == "logs":
        name = rest[0] if rest else "core"
        lines = rest[1] if len(rest) > 1 else "40"
        code, body = _call("GET", f"/v1/logs/{name}?lines={lines}")
        if code != 200:
            return _print_result(body)
        print("\n".join(body["lines"]))
        return 0
    if cmd == "config-get":
        code, body = _call("GET", "/v1/config/core")
        print(json.dumps(body, ensure_ascii=False, indent=2))
        return 0 if code == 200 else 1
    if cmd == "config-set":
        if not rest:
            print("用法: config-set KEY=VALUE ...")
            return 2
        fields = dict(kv.split("=", 1) for kv in rest)
        code, body = _call("PUT", "/v1/config/core", {"fields": fields})
        return _print_result(body)
    if cmd == "shutdown":
        code, body = _call("POST", "/v1/shutdown")
        return _print_result(body)
    print(f"未知命令 {cmd}（见文件头用法）")
    return 2


if __name__ == "__main__":
    sys.exit(main())
