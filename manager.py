#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
manager.py —— Abyss 管家（增量①：命令行监督器）

管理 Core 与 SnowLuma 两个进程：启动 / 停止 / 重启 / 状态 / 日志跟踪 / 打开网页。
Core 先于 SnowLuma 启动（连接有依赖）；Core 意外退出时看门狗 5 秒后自动拉起
（仅交互模式有效）。子进程输出重定向到 logs/manager/*.out.log，不用管道，
因此管家退出不会卡死子进程，也可以随时脱离（detach）。

用法：
    python manager.py                  # 交互模式（REPL，看门狗生效）
    python manager.py status           # 一次性命令：status/start/stop/restart/logs/open
    python manager.py start core       # target ∈ {core, snowluma, all}
    python manager.py logs core        # Ctrl+C 退出跟踪

REPL 命令：
    status | start <t> | stop <t> | restart <t> | logs <name> | open web|snowluma
    wait <秒>（脚本/测试用） | shutdown（停止全部并退出） | exit（脱离并退出）

注意：
    - stop 是硬停止（taskkill /T /F）：SQLite WAL 保证数据安全；
      增量② 会给 Core 加 /system/shutdown 走优雅停止
    - 「运行中(外部)」= 不是本管家启动的实例（比如你手动跑的 main.py），
      stop 会照样接管杀掉
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))
os_cwd = PROJECT_ROOT

LOG_DIR = PROJECT_ROOT / "logs" / "manager"
SNOWLUMA_DIR = PROJECT_ROOT / "SnowLuma"

CREATE_NEW_PROCESS_GROUP = 0x00000200
HEALTH_TIMEOUT_SEC = 30      # start 等健康检查的上限
WATCHDOG_RESTART_DELAY = 5   # 意外退出后延迟重启秒数
WATCHDOG_SCAN_INTERVAL = 2


def _core_ports() -> tuple[int, int]:
    """从 config 读 Core 的两个端口（走 .env.dev 覆盖层，与真实启动一致）。"""
    from config.adapt_settings import get_onebot_config, get_web_config
    return get_web_config().WS_PORT, get_onebot_config().WS_PORT


def _snowluma_webui_port() -> int:
    try:
        cfg = json.loads((SNOWLUMA_DIR / "config" / "runtime.json").read_text(encoding="utf-8"))
        return int(cfg.get("webuiPort", 5099))
    except Exception:
        return 5099


@dataclass
class ProcSpec:
    name: str
    cmd: list
    cwd: Path
    out_log: Path
    ports: list                    # 任一端口在监听即视为「活着」
    health_url: str = ""           # 可选 HTTP 健康检查（start 时等待它就绪）
    order: int = 0                 # start all 的顺序
    auto_restart: bool = True
    desc: str = ""


def build_specs() -> dict[str, ProcSpec]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    web_port, onebot_port = _core_ports()
    return {
        "core": ProcSpec(
            name="core",
            cmd=[sys.executable, "-u", "main.py"],
            cwd=PROJECT_ROOT,
            out_log=LOG_DIR / "core.out.log",
            ports=[web_port, onebot_port],
            health_url=f"http://127.0.0.1:{web_port}/health",
            order=0,
            desc="Abyss Core（大脑 + Web 5002 + OneBot 5001）",
        ),
        "snowluma": ProcSpec(
            name="snowluma",
            cmd=["cmd", "/c", "launcher.bat"],
            cwd=SNOWLUMA_DIR,
            out_log=LOG_DIR / "snowluma.out.log",
            ports=[_snowluma_webui_port(), 3001],
            order=1,
            auto_restart=False,   # 登录会话宝贵，崩溃后自动重启可能丢登录态，谨慎
            desc=f"SnowLuma（QQ 登录，WebUI http://127.0.0.1:{_snowluma_webui_port()}）",
        ),
    }


# ============================================================
# 探测工具
# ============================================================

def port_alive(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def spec_alive(spec: ProcSpec) -> bool:
    return any(port_alive(p) for p in spec.ports)


def pid_listening_on(port: int) -> int | None:
    """netstat 反查占用端口的 PID（用于接管/停止外部实例）。

    注意 netstat 输出是本地编码（中文 Windows 为 GBK），这里按字节拿、
    宽松解码——我们只需要 ASCII 的地址与 PID 列。
    """
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=10).stdout
        for line in out.decode("utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3] == "LISTENING" and f":{port}" in parts[1]:
                # 匹配 127.0.0.1:5001 或 0.0.0.0:5001；:5001 不会误中 :15001（含冒号前缀）
                return int(parts[4])
    except Exception:
        pass
    return None


def health_ok(spec: ProcSpec) -> bool:
    if not spec.health_url:
        return spec_alive(spec)
    try:
        with urllib.request.urlopen(spec.health_url, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def fmt_uptime(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ============================================================
# 管家主体
# ============================================================

@dataclass
class Managed:
    spec: ProcSpec
    proc: subprocess.Popen | None = None
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
            return (f"[{name}] 已在运行（{'本管家' if mine else '外部'}实例 pid={pid}），跳过启动")
        m.stop_requested = False
        spec.out_log.parent.mkdir(parents=True, exist_ok=True)
        # append 模式：历次启动的输出都保留在同一份 out log 里
        out = open(spec.out_log, "ab")
        try:
            proc = subprocess.Popen(
                spec.cmd, cwd=spec.cwd,
                stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                creationflags=CREATE_NEW_PROCESS_GROUP,
            )
        finally:
            out.close()   # 子进程已继承句柄，父进程即可关闭

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
        else:
            pid = pid_listening_on(spec.ports[0])
        if pid is None:
            with self._lock:
                m.proc = None
            return f"[{name}] 未在运行"
        with self._lock:
            m.stop_requested = True
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=15)
        deadline = time.time() + 10
        while spec_alive(spec) and time.time() < deadline:
            time.sleep(0.4)
        with self._lock:
            if mine:
                m.last_exit_code = m.proc.poll()
                m.proc = None
        state = "已停止" if not spec_alive(spec) else "进程已杀但端口未释放(等待中)"
        return f"[{name}] {state} (曾为 pid={pid}, {'本管家' if mine else '外部'})"

    def restart(self, name: str) -> list[str]:
        return [self.stop(name), self.start(name)]

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
        urls = {
            "web": self.specs["core"].health_url.replace("/health", ""),
            "snowluma": f"http://127.0.0.1:{_snowluma_webui_port()}",
        }
        if target not in urls:
            return f"未知目标 {target}（可选: web / snowluma）"
        webbrowser.open(urls[target])
        return f"已在浏览器打开 {urls[target]}"


# ============================================================
# REPL 与一次性命令
# ============================================================

HELP = """命令:
  status                 查看全部进程状态
  start <core|snowluma|all>
  stop  <core|snowluma|all>
  restart <core|snowluma>
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
                        choices=["status", "start", "stop", "restart", "logs", "open"],
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
