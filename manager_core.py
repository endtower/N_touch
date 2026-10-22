#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手动进程管理：串行启停、启动检查和退出确认，不自动恢复进程。

操作意图仅保存在内存。重复 start 共用同一版本，stop/restart 可取消
启动等待。退出检测只更新状态；只有用户命令可以创建新进程。
"""

from __future__ import annotations

import asyncio
import queue
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass

from paths import app_root, data_root, log_root
from core.supervisor import (
    ProcSpec,
    build_specs,
    core_cmd,
    ingress_cmd,
    spawn,
    stop_tree,
)

STATES = ("stopped", "starting", "running", "stopping", "failed", "conflict")

HEALTH_WAIT_SEC = 30
GRACEFUL_STOP_TIMEOUT = 10


# ============================================================
# 配置容错的规格构建
# ============================================================

def build_specs_safe() -> tuple[dict[str, ProcSpec], list[str]]:
    """Core 配置非法时降级为 core.exe/默认端口并记录错误，管理服务不崩。"""
    errors: list[str] = []
    try:
        return build_specs(), errors
    except Exception as e:
        errors.append(f"Core 配置加载失败（{type(e).__name__}: {e}），已降级运行")
    web_port, onebot_port = 5002, 5001
    try:
        from core.supervisor import core_ports
        web_port, onebot_port = core_ports()
    except Exception:
        pass
    from core.supervisor import snowluma_root, LOG_DIR
    specs = {
        "ingress": ProcSpec(
            name="ingress", cmd=ingress_cmd(), cwd=app_root(),
            out_log=LOG_DIR / "ingress.out.log", ports=[5003, onebot_port],
            health_url="http://127.0.0.1:5003/health", order=0,
            desc="消息接入（降级端口）",
        ),
        "core": ProcSpec(
            name="core", cmd=core_cmd(), cwd=app_root(),
            out_log=LOG_DIR / "core.out.log",
            ports=[web_port],
            health_url=f"http://127.0.0.1:{web_port}/health",
            order=2, desc="Abyss Core（降级端口）", dependencies=("ingress",),
        ),
        "snowluma": ProcSpec(
            name="snowluma", cmd=[str(snowluma_root() / "node.exe"), str(snowluma_root() / "index.mjs")], cwd=snowluma_root(),
            out_log=LOG_DIR / "snowluma.out.log",
            ports=[5099, 3001], order=1,
            desc="SnowLuma（降级端口）",
            dependencies=("ingress",),
        ),
    }
    return specs, errors


def _port_alive(port: int, timeout: float = 0.4) -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _health_ok(url: str) -> bool:
    if not url:
        return False
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


# ============================================================
# 目标控制器
# ============================================================

@dataclass
class TargetSnapshot:
    name: str
    state: str
    desired: str
    pid: int | None
    ports: list
    ready: bool
    managed: bool
    last_error: str
    uptime_sec: float | None


class TargetController:
    """单个目标的唯一控制者，只执行手动命令并检查已持有进程的退出。"""

    def __init__(self, spec: ProcSpec, service: "SupervisorService"):
        self.spec = spec
        self.service = service
        self.queue: queue.Queue = queue.Queue()
        self.state = "stopped"
        self.desired = "stopped"
        self.ready = False
        self.managed = False
        self.proc: subprocess.Popen | None = None
        self.started_at = 0.0
        self.last_error = ""
        self.epoch = 0
        self._last_command: str | None = None
        self._stop_event = threading.Event()
        self._worker = threading.Thread(target=self._run, daemon=True, name=f"ctl-{spec.name}")
        self._worker.start()

    # ---------- 对外命令：epoch 分配与入队原子化（复核 R3）----------

    def submit(self, kind: str, update_internal: bool = False) -> dict:
        if kind not in ("start", "stop", "restart"):
            return {"ok": False, "error_code": "bad_command", "message": f"未知命令 {kind}"}
        done = threading.Event()
        result: dict = {}
        with self.service.epoch_lock:
            guard = getattr(self.service, "update_guard", None)
            if not update_internal and guard and guard(self.spec.name):
                return {"ok": False, "error_code": "updating", "message": "组件正在更新，请完成或恢复更新后再操作"}
            # 相同 start/stop 不撤销进行中的相同操作。
            if kind != self._last_command or kind == "restart":
                self.epoch += 1
                self._last_command = kind
            epoch = self.epoch
            desired = {"start": "running", "stop": "stopped", "restart": "running"}[kind]
            self.desired = desired
            self.queue.put((kind, epoch, done, result, update_internal))
        health_wait = self.spec.startup_timeout or HEALTH_WAIT_SEC
        dependency_wait = sum((self.service.controller(name).spec.startup_timeout or HEALTH_WAIT_SEC)
                              for name in self.spec.dependencies if self.service.controller(name) is not None)
        done.wait(timeout=health_wait + dependency_wait + GRACEFUL_STOP_TIMEOUT + 20)
        if not result:
            return {"ok": False, "error_code": "timeout",
                    "message": "命令排队超时（目标正被其他操作占用），稍后重试"}
        return result

    def snapshot(self) -> TargetSnapshot:
        proc = self.proc
        alive = proc is not None and proc.poll() is None
        uptime = time.time() - self.started_at if (alive and self.started_at) else None
        return TargetSnapshot(
            name=self.spec.name, state=self.state, desired=self.desired,
            pid=proc.pid if alive else None, ports=list(self.spec.ports),
            ready=bool(alive and self.ready and self.state == "running"), managed=self.managed,
            last_error=self.last_error, uptime_sec=round(uptime, 1) if uptime else None,
        )

    # ---------- 工作线程：手动命令与被动退出检测 ----------

    def _run(self) -> None:
        while not self._stop_event.is_set():
            item = None
            try:
                item = self.queue.get(timeout=1.0)
            except queue.Empty:
                pass
            if item is not None:
                kind, epoch, done, result, update_internal = item
                try:
                    if epoch != self.epoch:
                        result.update({"ok": False, "error_code": "superseded",
                                       "state": self.state,
                                       "message": "命令已过时（有更新的操作生效）"})
                    elif kind == "start":
                        result.update(self._cmd_start(epoch, update_internal))
                    elif kind == "stop":
                        result.update(self._cmd_stop(epoch))
                    elif kind == "restart":
                        r1 = self._cmd_stop(epoch)
                        result.update(r1)
                        if r1.get("ok") or r1.get("error_code") == "not_running":
                            result.update(self._cmd_start(epoch, update_internal))
                except Exception as e:
                    result.update({"ok": False, "error_code": "internal",
                                   "message": f"{type(e).__name__}: {e}"})
                finally:
                    done.set()
                continue
            self._check_exit()

    # ---------- 命令实现 ----------

    def _refresh_spec(self) -> None:
        """每次 start 前重读配置（端口/路径变更 → 下一次启动即生效，复核 R4-4）。"""
        try:
            specs, errors = build_specs_safe()
            if self.spec.name in specs:
                self.spec = specs[self.spec.name]
        except Exception:
            pass

    def _cmd_start(self, epoch: int, update_internal: bool = False) -> dict:
        guard = getattr(self.service, "update_guard", None)
        if not update_internal and guard and guard(self.spec.name):
            return {"ok": False, "error_code": "updating", "message": "组件正在更新，启动已取消"}
        if epoch != self.epoch or self.desired != "running":
            return {"ok": False, "error_code": "superseded",
                    "state": self.state, "message": "启动已取消（有更新的操作生效）"}
        if self.proc is not None and self.proc.poll() is None:
            self.state = "running" if self.ready else "starting"
            return {"ok": True, "state": self.state, "pid": self.proc.pid,
                    "message": "目标进程已在（本服务管理），不重复创建"}
        self._refresh_spec()
        if any(_port_alive(p) for p in self.spec.ports):
            self.state = "conflict"
            self.last_error = "端口被外部程序占用（不是本服务启动的实例），不自动接管"
            return {"ok": False, "error_code": "conflict", "state": self.state,
                    "message": self.last_error}
        for name in self.spec.dependencies:
            dependency = self.service.controller(name)
            if dependency is None:
                return {"ok": False, "error_code": "missing_dependency", "message": f"缺少服务 {name}"}
            result = dependency.submit("start", update_internal)
            if not result.get("ok") or not dependency.ready:
                self.state = "failed"
                self.last_error = f"{name} 未就绪: {result.get('message', '')}"
                return {"ok": False, "error_code": "dependency_failed", "message": self.last_error}
        if epoch != self.epoch or self.desired != "running":
            return {"ok": False, "error_code": "superseded", "message": "启动已取消"}
        self.state = "starting"
        self.last_error = ""
        try:
            proc = spawn(self.spec, detached=False, clean_env=True)
        except Exception as e:
            self.state = "failed"
            self.last_error = f"spawn 失败: {e}"
            return {"ok": False, "error_code": "spawn_failed", "state": self.state,
                    "message": self.last_error}
        self.proc = proc
        self.started_at = time.time()
        self.managed = True
        # 健康等待可取消：每轮核对命令版本（复核 R3——不把 stop 困在等待里）
        health_wait = self.spec.startup_timeout or HEALTH_WAIT_SEC
        deadline = time.time() + health_wait
        while time.time() < deadline:
            if proc.poll() is not None:
                self.state = "failed"
                self.ready = False
                self.last_error = f"启动后立即退出 (exitcode={proc.returncode})"
                return {"ok": False, "error_code": "exited", "state": self.state,
                        "message": self.last_error}
            if epoch != self.epoch or self.desired != "running":
                # 有更新意图：终止刚拉起的进程（它属于本控制器），返回取消
                stopped = self._cmd_stop(epoch)
                if not stopped.get("ok"):
                    return stopped
                return {"ok": False, "error_code": "superseded", "state": self.state,
                        "message": "启动已取消（有更新的操作生效）"}
            if _health_ok(self.spec.health_url) if self.spec.health_url \
                    else any(_port_alive(p) for p in self.spec.ports):
                self.state = "running"
                self.ready = True
                return {"ok": True, "state": "running", "pid": proc.pid,
                        "message": "已启动并就绪"}
            time.sleep(0.4)
        self.state = "failed"
        self.ready = False
        self.last_error = f"{health_wait:g}s 内未就绪（进程保持追踪，可停止后重试）"
        return {"ok": False, "error_code": "not_ready", "state": self.state,
                "pid": proc.pid, "message": self.last_error}

    def _cmd_stop(self, epoch: int) -> dict:
        pid = self.proc.pid if (self.proc is not None and self.proc.poll() is None) else None
        self.state = "stopping"
        if pid is None:
            if any(_port_alive(p) for p in self.spec.ports):
                # 进程已死但端口仍开放：目标已停 + 外部占用，两件事分开说（复核 R2）
                self.state = "stopped"
                self.ready = False
                self.managed = False
                return {"ok": True, "error_code": "external_port",
                        "state": "stopped",
                        "message": "目标已停止，但端口仍被外部程序占用（未处理外部进程）"}
            self.state = "stopped"
            self.ready = False
            self.managed = False
            return {"ok": True, "error_code": "not_running", "state": "stopped",
                    "message": "目标未在运行"}
        # 优雅优先：core 走内部直达关闭入口（只让 uvicorn 自己退出，无回调循环）
        if self.spec.name in ("core", "ingress") and _health_ok(self.spec.health_url):
            try:
                base = self.spec.health_url.rsplit("/", 1)[0]
                token = (data_root() / "manager" / "service_token.txt").read_text(
                    encoding="utf-8").strip()
                shutdown_path = "/v1/shutdown" if self.spec.name == "ingress" else "/system/core/shutdown-internal"
                req = urllib.request.Request(base + shutdown_path,
                                             method="POST", headers={"X-Manager-Token": token})
                with urllib.request.urlopen(req, timeout=3):
                    pass
            except Exception:
                pass
        # 以退出事实确认（复核 R2）
        deadline = time.time() + GRACEFUL_STOP_TIMEOUT
        while time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None \
                    and not any(_port_alive(p) for p in self.spec.ports):
                break
            time.sleep(0.3)
        alive = self.proc is not None and self.proc.poll() is None
        if alive or any(_port_alive(p) for p in self.spec.ports):
            if alive:
                stop_tree(self.spec, self.proc.pid)   # 仅终止自己持有的进程
            deadline = time.time() + 8
            while any(_port_alive(p) for p in self.spec.ports) and time.time() < deadline:
                time.sleep(0.3)
        still_alive = self.proc is not None and self.proc.poll() is None
        if still_alive:
            # 停止失败：保留句柄与托管归属，返回失败与真实状态（复核 R2）
            self.state = "failed"
            self.last_error = "停止失败：进程在优雅等待与强制终止后仍然存活"
            return {"ok": False, "error_code": "stop_failed", "state": self.state,
                    "pid": self.proc.pid, "message": self.last_error}
        exited = self.proc.poll() if self.proc is not None else None
        self.proc = None
        self.managed = False
        self.ready = False
        self.state = "stopped"
        note = "" if not any(_port_alive(p) for p in self.spec.ports) \
            else "目标已退出，但端口仍被外部程序占用"
        return {"ok": True, "state": "stopped", "exited": exited,
                "message": ("已停止" + (f"（{note}）" if note else ""))}

    # ---------- 被动退出检测：报告失败，不创建进程 ----------

    def _check_exit(self) -> None:
        if self.proc is None or self.state == "stopping":
            return
        code = self.proc.poll()
        if code is None:
            # A failed stop can leave ready=True with state='failed'. Health is a
            # fact about the existing process, independent of the last command.
            if self.state in ("starting", "failed") and (
                    _health_ok(self.spec.health_url) if self.spec.health_url
                    else any(_port_alive(p) for p in self.spec.ports)):
                self.ready = True
                self.state = "running"
                if self.desired == "running":
                    self.last_error = ""
            return
        self.ready = False
        self.managed = False
        self.proc = None
        self.state = "failed"
        self.last_error = f"进程已退出 (exitcode={code})；请手动启动"


# ============================================================
# 管理服务
# ============================================================

class SupervisorService:
    def __init__(self) -> None:
        self.epoch_lock = threading.Lock()
        self.update_guard = None
        self.specs, self.config_errors = build_specs_safe()
        self.controllers = {n: TargetController(s, self) for n, s in self.specs.items()}

    def snapshots(self) -> list[TargetSnapshot]:
        return [c.snapshot() for c in self.controllers.values()]

    def controller(self, name: str) -> TargetController | None:
        return self.controllers.get(name)

    def stop_everything(self) -> list[str]:
        msgs = []
        for c in sorted(self.controllers.values(), key=lambda c: c.spec.order, reverse=True):
            msgs.append(f"[{c.spec.name}] " + c.submit("stop").get("message", ""))
        return msgs

    async def stop_everything_async(self) -> list[str]:
        return await asyncio.to_thread(self.stop_everything)
