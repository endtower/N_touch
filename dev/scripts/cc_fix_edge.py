#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""api.commandcode.ai Cloudflare 边缘 IP 体检 + 一键重钉 hosts。

用法:
  python cc_fix_edge.py check     # 只诊断, 不改任何东西
  python cc_fix_edge.py fix       # 诊断, 若当前钉的 IP 劣化则自动换最优 IP (会弹 UAC)
  python cc_fix_edge.py fix --force
                                  # 无论当前好坏, 强制重钉到最优 IP
背景:
  该域名走 Cloudflare, 不同边缘 IP 从本机出发的路由质量差异很大且会随时间变化,
  钉一个劣化 IP 会导致 TLS 握手超时、请求莫名卡住十几秒。
"""
import ctypes, os, re, socket, ssl, statistics, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

HOST = "api.commandcode.ai"
HOSTS = r"C:\Windows\System32\drivers\etc\hosts"
KEY = "user_RQdkdzSVgqCU3zNPsr18cLMAdADQdSRHGGauYLCLqfPvZp6tTvNgUaF68mAzSkhYqVNRJv7qzawt1VWKLUELeV6"
CTX = ssl.create_default_context()
# CF anycast 候选边缘; 任何 CF 边缘都能按 SNI 服务本域名, 哪个快用哪个
CANDIDATES = ["104.17.135.117", "104.17.34.86", "104.16.132.229", "104.16.5.34",
              "172.64.149.23", "104.21.49.202", "104.17.147.22", "104.16.0.34",
              "188.114.96.3", "162.159.140.229"]
TRIES, TIMEOUT = 3, 4          # 每个 IP 的握手尝试次数 / 单次超时秒数
PIN_RE = re.compile(r"^\s*\d{1,3}(?:\.\d{1,3}){3}\s+" + re.escape(HOST) + r"\s*$")


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def read_hosts():
    raw = open(HOSTS, "rb").read()
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", "replace"), "utf-8"


def current_pin():
    text, _ = read_hosts()
    for line in text.splitlines():
        if PIN_RE.match(line):
            return line.split()[0]
    return None


def handshake(ip, timeout=TIMEOUT):
    """TCP+TLS 到指定 IP (SNI=域名), 返回总秒数, 失败返回 None。"""
    try:
        t0 = time.perf_counter()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((ip, 443))
        ss = CTX.wrap_socket(s, server_hostname=HOST)
        ss.close()
        return time.perf_counter() - t0
    except Exception:
        return None


def scan_ip(ip):
    times = [handshake(ip) for _ in range(TRIES)]
    ok = [t for t in times if t is not None]
    return {"ip": ip, "ok": len(ok), "median": statistics.median(ok) if ok else None}


def scan_all(ips):
    with ThreadPoolExecutor(max_workers=len(ips)) as ex:
        return sorted(ex.map(scan_ip, ips),
                      key=lambda r: (-r["ok"], r["median"] if r["median"] is not None else 99))


def api_ok(ip):
    """端到端验证该 IP 真的能服务本域名: TLS + GET /provider/v1/models。"""
    try:
        raw = socket.create_connection((ip, 443), timeout=8)
        ss = CTX.wrap_socket(raw, server_hostname=HOST)
        req = (f"GET /provider/v1/models HTTP/1.1\r\nHost: {HOST}\r\n"
               f"Authorization: Bearer {KEY}\r\nUser-Agent: cc_fix_edge\r\nConnection: close\r\n\r\n")
        ss.sendall(req.encode())
        buf = b""
        while True:
            d = ss.recv(65536)
            if not d:
                break
            buf += d
        ss.close()
        return buf.startswith(b"HTTP/") and b'200' in buf.split(b"\r\n", 1)[0] \
            and b'"object":"list"' in buf
    except Exception:
        return False


def healthy(stat):
    return stat["ok"] == TRIES and stat["median"] is not None and stat["median"] <= 1.0


def apply_pin(ip, reason):
    """改 hosts (需要管理员): 删掉本域名旧钉行, 追加新钉行, 刷新 DNS 缓存。"""
    text, enc = read_hosts()
    nl = "\r\n" if "\r\n" in text else "\n"
    kept = [ln for ln in text.splitlines() if not PIN_RE.match(ln)]
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    kept += [f"# {stamp}: re-pinned by cc_fix_edge.py ({reason}): "
             f"{ip} = {TRIES}/{TRIES} ok", f"{ip} {HOST}"]
    with open(HOSTS, "w", encoding=enc, newline="") as f:
        f.write(nl.join(kept) + nl)
    subprocess.run(["ipconfig", "/flushdns"], capture_output=True)


def elevate_apply(ip, reason):
    """以管理员重跑自身 --apply, 弹 UAC。返回退出码。"""
    ps1 = os.path.join(os.environ.get("TEMP", "."), "cc_fix_edge_apply.ps1")
    py = sys.executable.replace("'", "''")
    scr = os.path.abspath(__file__).replace("'", "''")
    with open(ps1, "w") as f:
        f.write(f"& '{py}' '{scr}' --apply {ip} --reason '{reason}'\nexit $LASTEXITCODE\n")
    cmd = ["powershell", "-NoProfile", "-Command",
           "try { $p = Start-Process powershell -Verb RunAs -Wait -PassThru -WindowStyle Hidden "
           f"-ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','{ps1}'; "
           "exit $p.ExitCode } catch { exit 42 }"]
    return subprocess.run(cmd).returncode


def verify():
    ips = sorted({i[4][0] for i in socket.getaddrinfo(HOST, 443, proto=socket.IPPROTO_TCP)
                  if i[0] == socket.AF_INET})
    times = [handshake(ip) for ip in ips for _ in range(5)]
    ok = [t for t in times if t is not None]
    print(f"  域名现在解析到: {', '.join(ips)}")
    print(f"  握手 {len(ok)}/5 成功"
          + (f", 中位 {statistics.median(ok):.2f}s" if ok else ""))


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    force = "--force" in sys.argv
    if "--apply" in sys.argv:                      # 提权后的内部入口
        i = sys.argv.index("--apply")
        ip = sys.argv[i + 1]
        r = sys.argv[sys.argv.index("--reason") + 1] if "--reason" in sys.argv else "fix"
        assert api_ok(ip), f"candidate {ip} failed end-to-end check, hosts not touched"
        apply_pin(ip, r)
        sys.exit(0)

    print(f"== {HOST} 边缘体检 ({datetime.now():%Y-%m-%d %H:%M}) ==")
    pin = current_pin()
    results = scan_all(CANDIDATES + ([pin] if pin and pin not in CANDIDATES else []))
    print(f"{'IP':<20}{'握手成功率':<12}{'中位延迟'}")
    for r in results:
        tag = "  <- 当前钉的" if r["ip"] == pin else ""
        med = "FAIL" if r["median"] is None else f'{r["median"]:.2f}s'
        print(f"{r['ip']:<20}{r['ok']}/{TRIES:<11}{med}{tag}")

    good = [r for r in results if healthy(r)]
    if not good:
        print("\n[!] 没有任何候选 IP 达到健康标准 (全部握手成功且中位 <=1s)。")
        print("    可能是整体网络故障, 此时不建议改 hosts, 稍后再试。")
        sys.exit(1)

    best = good[0]
    if not api_ok(best["ip"]):
        print(f"\n[!] 最优 IP {best['ip']} 通过不了 API 端到端验证, 不改 hosts。")
        sys.exit(1)

    pin_stat = next((r for r in results if r["ip"] == pin), None)
    if mode == "check":
        if pin and pin_stat and healthy(pin_stat):
            print(f"\n[OK] 当前钉的 {pin} 健康 (中位 {pin_stat['median']:.2f}s), 无需处理。")
        elif pin:
            print(f"\n[!] 当前钉的 {pin} 已劣化, 建议运行: python {os.path.basename(__file__)} fix")
        else:
            print(f"\n[!] 域名未钉定, 建议运行: python {os.path.basename(__file__)} fix")
        sys.exit(0)

    if pin and pin_stat and healthy(pin_stat) and not force:
        print(f"\n[OK] 当前钉的 {pin} 健康, 无需重钉 (强制重钉加 --force)。")
        sys.exit(0)

    reason = (f"was {pin}, {'degraded' if pin else 'unpinned'}")
    print(f"\n--> 重钉到 {best['ip']} ({best['ok']}/{TRIES}, 中位 {best['median']:.2f}s) ...")
    if is_admin():
        assert api_ok(best["ip"])
        apply_pin(best["ip"], reason)
    else:
        code = elevate_apply(best["ip"], reason)
        if code != 0:
            print(f"[!] 提权失败 (退出码 {code}, 42 = UAC 被取消)。hosts 未改动。")
            sys.exit(code)
    time.sleep(1)
    print("\n== 验证 ==")
    verify()
    print("[OK] 完成。若之后又出现卡顿, 重新运行本脚本即可。")


if __name__ == "__main__":
    main()
