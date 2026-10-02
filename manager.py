#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
manager.py —— Abyss 管家（命令行监督器）

管理 Core 与 SnowLuma 两个进程：启动 / 停止 / 重启 / 状态 / 日志跟踪 / 打开网页。
Core 先于 SnowLuma 启动（连接有依赖）；Core 意外退出时看门狗 5 秒后自动拉起
（仅交互模式有效）。子进程输出重定向到 logs/manager/*.out.log（不用管道），
因此管家退出不会卡死子进程，也可以随时脱离（detach）。

用法：
    python manager.py                  # 交互模式（REPL，看门狗生效）
    python manager.py status           # 一次性命令：status/start/stop/restart/logs/open/respawn
    python manager.py start core       # target ∈ {core, snowluma, all}
    python manager.py respawn core     # 等端口释放后拉起（/system/core/restart 的幕后工人）
    python manager.py logs core        # Ctrl+C 退出跟踪

REPL 命令：
    status | start <t> | stop <t> | restart <t> | logs <name> | open web|snowluma
    wait <秒>（脚本/测试用） | shutdown（停止全部并退出） | exit（脱离并退出）

注意：
    - stop 是硬停止（taskkill /T /F）：SQLite WAL 保证数据安全；优雅关闭走
      Core 的 /system/core/shutdown（网页系统面板）
    - 「运行中(外部)」= 不是本管家启动的实例（比如你手动跑的 main.py），
      stop 会照样接管杀掉
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.supervisor import (   # noqa: E402
    ProcSpec,
    build_specs,
    health_ok,
    pid_listening_on,
    port_alive,
    spawn,
    spec_alive,
    stop_tree,
    wait_ports_free,
)

HEALTH_TIMEOUT_SEC = 30      # start 等健康检查的上限
WATCHDOG_RESTART_DELAY = 5   # 意外退出后延迟重启秒数
WATCHDOG_SCAN_INTERVAL = 2


# ============================================================
# 管家主体
# ============================================================

@dataclass
class Managed:
    spec: ProcSpec
    proc: object | None = None
    started_at: float = 0.0
    stop_requested: bool = False
    restarts: int = 0
    last_exit_code: int | None = None


class Manager:
    def __init__(self) -> None:
        self.specs = build_specs()
        self.managed: dict[str, Managed] = {n: Managed(s) for n, s in self.specs.items()}
        self._lock = threading.Lock()
        self._watchdog: threading.Thread | None = None

    # ---------------- 启停 ----------------

    def start(self, name: str) -> str:
        m = self.managed[name]
        spec = m.spec
        if spec_alive(spec):
            pid = pid_listening_on(spec.ports[0])
            mine = m.proc is not None and m.proc.poll() is None
            return f"[{name}] 已在运行（{'本管家' if mine else '外部'}实例 pid={pid}），跳过启动"
        m.stop_requested = False
        proc = spawn(spec, detached=False)

        with self._lock:
            m.proc = proc
            m.started_at = time.time()
            m.last_exit_code = None

        # 等健康/端口就绪，让「start 返回 = 可用」
        deadline = time.time() + HEALTH_TIMEOUT_SEC
        while time.time() < deadline:
            if proc.poll() is not None:
                return f"[{name}] 启动后立即退出 (exitcode={proc.returncode})，详见 {spec.out_log.name}"
            if health_ok(spec):
                return f"[{name}] 已启动并就绪 (pid={proc.pid})"
            time.sleep(0.5)
        return (f"[{name}] 进程已拉起 (pid={proc.pid})，但 {HEALTH_TIMEOUT_SEC}s 内未就绪——"
                f"SnowLuma 未登录属正常；详见 {spec.out_log.name}")

    def stop(self, name: str) -> str:
        m = self.managed[name]
        spec = m.spec
        pid = None
        mine = False
        if m.proc is not None and m.proc.poll() is None:
            pid, mine = m.proc.pid, True
        with self._lock:
            m.stop_requested = True
        freed, killed = stop_tree(spec, pid)
        with self._lock:
            if mine and m.proc is not None:
                m.last_exit_code = m.proc.poll()
                m.proc = None
        state = "已停止" if freed else "已发送停止但端口未释放(等待中)"
        return f"[{name}] {state} (曾为 pid={killed}, {'本管家' if mine else '外部'})"

    def restart(self, name: str) -> list[str]:
        return [self.stop(name), self.start(name)]

    def respawn(self, name: str) -> str:
        """等目标进程的端口释放（比如 Core 正在优雅关闭），然后拉起。"""
        spec = self.specs[name]
        if wait_ports_free(spec, timeout=40):
            return self.start(name)
        return f"[{name}] 等待 {40}s 后端口仍被占用，放弃拉起（是否已有别的实例？）"

    def start_all(self) -> list[str]:
        msgs = []
        for spec in sorted(self.specs.values(), key=lambda s: s.order):
            msgs.append(self.start(spec.name))
        return msgs

    def stop_all(self) -> list[str]:
        msgs = []
        for spec in sorted(self.specs.values(), key=lambda s: s.order, reverse=True):
            msgs.append(self.stop(spec.name))
        return msgs

    # ---------------- 状态 ----------------

    def status_lines(self) -> list[str]:
        lines = [f"{'名称':<9} {'状态':<12} {'pid':<7} {'端口':<12} {'运行时长':<9} 说明"]
        for m in self.managed.values():
            spec = m.spec
            alive = spec_alive(spec)
            mine = m.proc is not None and m.proc.poll() is None
            if mine:
                state, pid = "运行中(托管)", m.proc.pid
            elif alive:
                state, pid = "运行中(外部)", pid_listening_on(spec.ports[0]) or "-"
            else:
                state, pid = "已停止", "-"
            uptime = fmt_uptime(time.time() - m.started_at) if mine else "-"
            extra = f"重启{m.restarts}次" if m.restarts else spec.desc
            lines.append(f"{spec.name:<9} {state:<12} {str(pid):<7} "
                         f"{','.join(map(str, spec.ports)):<12} {uptime:<9} {extra}")
        return lines

    # ---------------- 看门狗 ----------------

    def start_watchdog(self) -> None:
        def loop():
            while True:
                time.sleep(WATCHDOG_SCAN_INTERVAL)
                to_restart = []
                with self._lock:
                    for m in self.managed.values():
                        if m.proc is None or m.stop_requested or not m.spec.auto_restart:
                            continue
                        code = m.proc.poll()
                        if code is None:
                            continue
                        if time.time() - m.started_at < 10:
                            # 启动即退（崩溃循环），不自动拉起，留给人工排查
                            continue
                        m.restarts += 1
                        m.last_exit_code = code
                        m.stop_requested = False
                        to_restart.append(m)
                # 锁外执行重启（start 的健康等待可能长达 30s，不能占锁）
                for m in to_restart:
                    print(f"[watchdog] {m.spec.name} 意外退出 "
                          f"(exitcode={m.last_exit_code})，{WATCHDOG_RESTART_DELAY}s 后重启",
                          flush=True)
                    time.sleep(WATCHDOG_RESTART_DELAY)
                    print(self.start(m.spec.name), flush=True)

        self._watchdog = threading.Thread(target=loop, daemon=True, name="watchdog")
        self._watchdog.start()

    # ---------------- 日志跟踪 ----------------

    @staticmethod
    def tail(path: Path, last_bytes: int = 4096) -> None:
        """类似 tail -f：先打末尾若干字节，再持续追加。Ctrl+C 返回。"""
        if not path.exists():
            print(f"(尚无输出: {path})")
            return
        data = path.read_bytes()[-last_bytes:]
        sys.stdout.write(data.decode("utf-8", errors="replace"))
        sys.stdout.flush()
        pos = path.stat().st_size
        print(f"--- 跟踪中: {path.name}（Ctrl+C 返回）---")
        try:
            while True:
                size = path.stat().st_size
                if size > pos:
                    with open(path, "rb") as f:
                        f.seek(pos)
                        chunk = f.read(size - pos)
                    sys.stdout.write(chunk.decode("utf-8", errors="replace"))
                    sys.stdout.flush()
                    pos = size
                elif size < pos:   # 日志被轮转/清空
                    pos = 0
                time.sleep(0.4)
        except KeyboardInterrupt:
            print("(已退出跟踪)")

    # ---------------- 打开网页 ----------------

    def open_url(self, target: str) -> str:
        from core.supervisor import snowluma_webui_port
        urls = {
            "web": self.specs["core"].health_url.replace("/health", ""),
            "snowluma": f"http://127.0.0.1:{snowluma_webui_port()}",
        }
        if target not in urls:
            return f"未知目标 {target}（可选: web / snowluma）"
        webbrowser.open(urls[target])
        return f"已在浏览器打开 {urls[target]}"


def fmt_uptime(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ============================================================
# REPL 与一次性命令
# ============================================================

HELP = """命令:
  status                 查看全部进程状态
  start <core|snowluma|all>
  stop  <core|snowluma|all>
  restart <core|snowluma>
  respawn <core>         等端口释放后拉起（Core 优雅关闭期间的幕后工人）
  logs <core|snowluma>   跟踪进程输出（Ctrl+C 返回）
  open <web|snowluma>    浏览器打开前端 / SnowLuma WebUI
  wait <秒>              （脚本用）暂停
  shutdown               停止全部进程并退出管家
  exit                   退出管家（进程保持运行）"""


def repl(mgr: Manager) -> None:
    mgr.start_watchdog()
    print("Abyss 管家（交互模式，看门狗运行中）。help 查看命令，exit 脱离退出。")
    print("\n".join(mgr.status_lines()))
    while True:
        try:
            raw = input("manager> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n(退出，进程保持运行)")
            return
        if not raw:
            continue
        parts = raw.split()
        cmd, args = parts[0].lower(), parts[1:]
        try:
            if cmd in ("exit", "quit"):
                print("(退出，进程保持运行)")
                return
            elif cmd == "shutdown":
                print("\n".join(mgr.stop_all()))
                print("(全部已停止，退出)")
                return
            elif cmd == "help":
                print(HELP)
            elif cmd == "status":
                print("\n".join(mgr.status_lines()))
            elif cmd == "start":
                msgs = mgr.start_all() if (not args or args[0] == "all") else [mgr.start(args[0])]
                print("\n".join(msgs))
            elif cmd == "stop":
                msgs = mgr.stop_all() if (not args or args[0] == "all") else [mgr.stop(args[0])]
                print("\n".join(msgs))
            elif cmd == "restart":
                if not args:
                    print("用法: restart <core|snowluma>")
                else:
                    print("\n".join(mgr.restart(args[0])))
            elif cmd == "respawn":
                if not args:
                    print("用法: respawn <core>")
                else:
                    print(mgr.respawn(args[0]))
            elif cmd == "logs":
                if not args:
                    print("用法: logs <core|snowluma>")
                else:
                    mgr.tail(mgr.managed[args[0]].spec.out_log)
            elif cmd == "open":
                print(mgr.open_url(args[0] if args else ""))
            elif cmd == "wait":
                time.sleep(float(args[0]) if args else 1)
            else:
                print(f"未知命令 {cmd}（help 查看命令）")
        except Exception as e:
            print(f"[错误] {type(e).__name__}: {e}")


def one_shot(mgr: Manager, cmd: str, target: str | None) -> int:
    if cmd == "status":
        print("\n".join(mgr.status_lines()))
        return 0
    if cmd == "start":
        msgs = mgr.start_all() if (not target or target == "all") else [mgr.start(target)]
    elif cmd == "stop":
        msgs = mgr.stop_all() if (not target or target == "all") else [mgr.stop(target)]
    elif cmd == "restart":
        msgs = mgr.restart(target) if target else ["用法: restart <core|snowluma>"]
    elif cmd == "respawn":
        msgs = [mgr.respawn(target or "core")]
    elif cmd == "open":
        msgs = [mgr.open_url(target or "")]
    else:
        print(f"一次性模式不支持 {cmd}（logs 请用交互模式；或不带参数进入 REPL）")
        return 2
    print("\n".join(msgs))
    return 0


def main() -> int:
    # 统一输出编码：控制台代码页可能是 GBK，强制 UTF-8 保证中文与日志 tail 不乱码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser(description="Abyss 管家：Core / SnowLuma 进程监督")
    parser.add_argument("command", nargs="?", default=None,
                        choices=["status", "start", "stop", "restart", "respawn", "logs", "open"],
                        help="不带命令则进入交互模式")
    parser.add_argument("target", nargs="?", default=None,
                        help="core / snowluma / all（open: web / snowluma）")
    args = parser.parse_args()
    mgr = Manager()
    if args.command is None:
        repl(mgr)
        return 0
    if args.command == "logs":
        name = args.target or "core"
        mgr.tail(mgr.managed[name].spec.out_log)
        return 0
    return one_shot(mgr, args.command, args.target)


if __name__ == "__main__":
    sys.exit(main())
