#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
test_llm_connectivity — LLM 接入点连通性诊断（长期保留，勿删）

背景：代理（Ninja Client / Clash 内核）开关前后，commandcode 中转时常连不上，
现象是「开着连不上、关了也连不上」，需要一键定位到底断在哪一环。

本脚本按请求链路的顺序逐段探针，定位断点：

  0. 环境快照   系统代理开关(ProxyEnable/ProxyServer)、DNS 服务器、hosts 里的目标域名条目
  1. DNS 解析   socket.getaddrinfo（走系统解析器，会读 hosts）→ 解析出的 IP
  2. HTTPS 可达 GET {base_url}/models → 状态码 / Cloudflare 响应头 / 耗时
  3. 非流式对话 POST /chat/completions → 是否有 content / reasoning / usage
  4. 流式对话   POST /chat/completions (stream) → 首字延迟 / chunk 数 / 是否收到内容

目标接入点全部取自 config/llm_settings.py（唯一配置入口），不重复硬编码 key。

用法：
  python scripts/test_llm_connectivity.py                # 测全部接入点
  python scripts/test_llm_connectivity.py commandcode    # 只测名字含 commandcode 的接入点
  python scripts/test_llm_connectivity.py pick           # 优选 IP（输出 hosts 建议行，不自动改配置）

约定（与 AGENTS.md 一致）：
  - 全异步；
  - 探针失败绝不吞错，失败信息会完整记录并打印，脚本以退出码 1 结束；
    单个探针失败不中断后续探针（诊断脚本需要尽可能多的现场信息）。
"""

from __future__ import annotations

import asyncio
import os
import socket
import statistics
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402
from openai import AsyncOpenAI  # noqa: E402

from config.llm_settings import LLMSettings  # noqa: E402

TIMEOUT = 30.0
TEST_MESSAGES: List[Dict[str, str]] = [{"role": "user", "content": "回复两个字：收到"}]
# 真 Cloudflare anycast IP 候选（api.commandcode.ai）；换 IP 后在这行登记，供 DNS 探针比对污染
KNOWN_GOOD_IPS = ("172.67.167.23", "104.21.49.202")
# 阿里公共 DNS 的 DoH JSON 接口（国内可直连，解析结果无污染），用于优选模式取真 IP
DOH_URL = "https://223.5.5.5/resolve"

RED, GREEN, YELLOW, DIM, RESET = "\033[31m", "\033[32m", "\033[33m", "\033[2m", "\033[0m"


class Probe:
    """单个探针的结果记录。"""

    def __init__(self, name: str):
        self.name = name
        self.ok = False
        self.detail = ""
        self.extra: Dict[str, Any] = {}
        self.elapsed = 0.0

    def __repr__(self) -> str:
        mark = f"{GREEN}PASS{RESET}" if self.ok else f"{RED}FAIL{RESET}"
        return f"[{mark}] {self.name} ({self.elapsed:.2f}s) {self.detail}"


async def probe(probe_obj: Probe, coro) -> Probe:
    """执行探针协程并把异常原样记录进 Probe（不中断整个诊断流程，最终由退出码反映）。"""
    start = time.perf_counter()
    try:
        await coro
        probe_obj.ok = True
    except Exception as exc:  # 诊断脚本：失败必须被记录下来，绝不静默
        probe_obj.ok = False
        probe_obj.detail = f"{type(exc).__name__}: {exc}"
    probe_obj.elapsed = time.perf_counter() - start
    return probe_obj


# ==================================================
# 0. 环境快照
# ==================================================
def snapshot_environment() -> None:
    """打印与连通性直接相关的本机状态（只读，不修改任何配置）。"""
    print(f"\n{DIM}── 环境快照 ──{RESET}")

    if sys.platform == "win32":
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            def read(name: str) -> Any:
                try:
                    return winreg.QueryValueEx(key, name)[0]
                except FileNotFoundError:
                    return None

            proxy_enable = read("ProxyEnable")
            print(f"  系统代理 ProxyEnable = {proxy_enable}")
            print(f"  ProxyServer          = {read('ProxyServer')}")
            print(f"  AutoConfigURL        = {read('AutoConfigURL')}")
        if proxy_enable:
            print(f"  {YELLOW}注意：系统代理已开启，请求会走代理{RESET}")
    else:
        print("  （非 Windows，跳过系统代理读取）")

    hosts_path = Path(r"C:\Windows\System32\drivers\etc\hosts") if sys.platform == "win32" else Path("/etc/hosts")
    entries = [
        line.strip()
        for line in hosts_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    print(f"  hosts 生效条目 {len(entries)} 条，其中命中目标域名的：")
    hit = False
    for line in entries:
        if "commandcode" in line or "opencode" in line:
            print(f"    {line}")
            hit = True
    if not hit:
        print("    （无）")


# ==================================================
# 1. DNS 解析
# ==================================================
async def probe_dns(p: Probe, host: str) -> None:
    infos = await asyncio.to_thread(
        socket.getaddrinfo, host, 443, socket.AF_UNSPEC, socket.SOCK_STREAM
    )
    ips = sorted({info[4][0] for info in infos})
    p.detail = f"{host} → {', '.join(ips)}"
    p.extra["ips"] = ips
    if host.endswith("commandcode.ai") and not (set(ips) & set(KNOWN_GOOD_IPS)):
        raise RuntimeError(
            f"解析结果不含任何已知真 Cloudflare IP {KNOWN_GOOD_IPS}，疑似 DNS 污染"
        )


# ==================================================
# 2. HTTPS 可达性
# ==================================================
async def probe_https(p: Probe, base_url: str) -> None:
    url = base_url.rstrip("/") + "/models"
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(url)
    p.detail = (
        f"GET {url} → HTTP {resp.status_code} "
        f"(server={resp.headers.get('server')}, "
        f"cf-ray={resp.headers.get('cf-ray')}, {len(resp.content)}B)"
    )
    p.extra["status"] = resp.status_code
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")


# ==================================================
# 3/4. 对话（非流式 / 流式）
# ==================================================
def build_client(api_key: str, base_url: str) -> AsyncOpenAI:
    """构造与项目 LLMBridge 完全同构的客户端（含覆盖 UA）。

    opencode Go 网关强制要求会话头（缺失即 400 MissingSessionID），
    与 utils/llm_client_manager.py 的 LLMClient 行为保持一致。
    """
    headers = {"User-Agent": "N-Touch/1.0"}
    if "opencode" in base_url:
        headers["x-opencode-session"] = uuid.uuid4().hex
    return AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        default_headers=headers,
        timeout=TIMEOUT,
    )


async def probe_chat(p: Probe, client: AsyncOpenAI, model: str) -> None:
    resp = await client.chat.completions.create(model=model, messages=TEST_MESSAGES)
    msg = resp.choices[0].message
    content = (msg.content or "").strip()
    reasoning = ""
    for attr in ("reasoning_content", "reasoning", "thinking"):
        value = getattr(msg, attr, None)
        if isinstance(value, str) and value.strip():
            reasoning = value.strip()
            break
    usage = resp.usage
    p.detail = (
        f"model={model} content={content[:40]!r} reasoning={len(reasoning)}字 "
        f"usage={usage.total_tokens if usage else None}"
    )
    p.extra["content"] = content
    if not content and not reasoning:
        raise RuntimeError("HTTP 200 但 content 与 reasoning 均为空（中转吞输出）")


async def probe_stream(p: Probe, client: AsyncOpenAI, model: str) -> None:
    start = time.perf_counter()
    chunks = 0
    first_token_at: Optional[float] = None
    content: List[str] = []
    reasoning_len = 0
    stream = await client.chat.completions.create(
        model=model, messages=TEST_MESSAGES, stream=True
    )
    async for chunk in stream:
        chunks += 1
        if first_token_at is None:
            first_token_at = time.perf_counter() - start
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta is None:
            continue
        if delta.content:
            content.append(delta.content)
        for attr in ("reasoning_content", "reasoning"):
            value = getattr(delta, attr, None)
            if isinstance(value, str):
                reasoning_len += len(value)
    text = "".join(content).strip()
    p.detail = (
        f"chunks={chunks} TTFT={first_token_at:.2f}s "
        f"content={text[:40]!r} reasoning={reasoning_len}字"
    )
    p.extra["content"] = text
    if not text and reasoning_len == 0:
        raise RuntimeError("流式返回 200 但未收到任何内容")


# ==================================================
# 优选模式：直连 Cloudflare 时挑出最快的 IP（更新 hosts 用）
# ==================================================
async def resolve_real_ips(host: str) -> List[str]:
    """经阿里 DoH 取真实 A 记录（绕开被污染的系统解析）。"""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            DOH_URL,
            params={"name": host, "type": "A"},
            headers={"accept": "application/dns-json"},
        )
    resp.raise_for_status()
    ips = sorted({a["data"] for a in resp.json().get("Answer", []) if a.get("type") == 1})
    if not ips:
        raise RuntimeError(f"DoH 未返回 {host} 的 A 记录")
    return ips


async def measure_ip(host: str, ip: str, path: str, rounds: int = 3) -> List[Tuple[str, float]]:
    """用 curl --resolve 强制走指定 IP 请求，返回逐轮 (http_code, 耗时秒)。

    Cloudflare anycast 各 IP 的国际出口质量差异极大（实测同一域名
    0.5s vs 30s 超时），故优选必须在真实 IP 上实测而非依赖 DNS 顺序。
    """
    samples: List[Tuple[str, float]] = []
    for _ in range(rounds):
        proc = await asyncio.create_subprocess_exec(
            "curl.exe", "-s", "-o", os.devnull,
            "--resolve", f"{host}:443:{ip}",
            "-w", "%{http_code} %{time_total}",
            "--max-time", str(int(TIMEOUT)),
            f"https://{host}{path}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        code, _, total = out.decode(errors="ignore").strip().partition(" ")
        samples.append((code, float(total) if total else TIMEOUT))
    return samples


async def pick_ip(host: str, path: str) -> int:
    print(f"\n{'=' * 70}\n>>> IP 优选：{host}\n{'=' * 70}")
    ips = await resolve_real_ips(host)
    print(f"  DoH(223.5.5.5) 解析 → {', '.join(ips)}")

    results: List[Tuple[float, int, str, str]] = []
    for ip in ips:
        samples = await measure_ip(host, ip, path)
        ok = sum(1 for code, _ in samples if code == "200")
        detail = " ".join(f"{code}/{t:.2f}s" for code, t in samples)
        results.append((statistics.median([t for _, t in samples]), ok, ip, detail))

    results.sort()
    print()
    for median, ok, ip, detail in results:
        print(f"  {ip:<16} 成功 {ok}/3  中位 {median:.2f}s   [{detail}]")

    best_median, best_ok, best_ip, _ = results[0]
    if best_ok == 0:
        print(f"\n{RED}所有候选 IP 均失败：直连链路当前不可用，需换接入点或走代理。{RESET}")
        return 1
    print(f"\n  最优 IP：{best_ip}（中位 {best_median:.2f}s，成功 {best_ok}/3）")
    print(f"  hosts 建议行：{best_ip} {host}")
    # 机器可读输出：供 scripts/fix_commandcode_hosts.ps1 解析，规避中文控制台编码差异
    print(f"PICKED={best_ip} HOST={host} MEDIAN={best_median:.2f}")
    return 0


# ==================================================
# 主流程
# ==================================================
TARGETS: List[Tuple[str, str, str, str]] = [
    # (显示名, base_url, api_key, 模型名)
    (
        "主力接入点",
        LLMSettings.FLASH_BASE_URL,
        LLMSettings.FLASH_API_KEY,
        LLMSettings.FLASH,
    ),
    (
        "opencode 统一",
        LLMSettings.BASE_URL,
        LLMSettings.API_KEY,
        LLMSettings.FLASH,
    ),
]


async def check_target(name: str, base_url: str, api_key: str, model: str) -> List[Probe]:
    host = urlparse(base_url).hostname
    print(f"\n{'=' * 70}\n>>> {name}  {base_url}  model={model}\n{'=' * 70}")

    client = build_client(api_key, base_url)
    steps = [
        ("DNS 解析", probe_dns, host),
        ("HTTPS 可达", probe_https, base_url),
        ("非流式对话", probe_chat, client, model),
        ("流式对话", probe_stream, client, model),
    ]

    results: List[Probe] = []
    for step in steps:
        label, func, *args = step
        r = Probe(label)
        results.append(await probe(r, func(r, *args)))
        print(f"  {r}")

    await client.close()
    return results


async def main() -> int:
    keyword = sys.argv[1].lower() if len(sys.argv) > 1 else None

    # 优选模式：只选 IP，不改任何配置（hosts 仍需人工更新）
    if keyword == "pick":
        parsed = urlparse(LLMSettings.FLASH_BASE_URL)
        return await pick_ip(parsed.hostname, parsed.path.rstrip("/") + "/models")

    snapshot_environment()

    all_results: List[Probe] = []
    for name, base_url, api_key, model in TARGETS:
        if keyword and keyword not in name.lower() and keyword not in base_url.lower():
            continue
        if not base_url or not api_key:
            print(f"\n（跳过 {name}：接入点未配置）")
            continue
        all_results.extend(await check_target(name, base_url, api_key, model))

    print(f"\n{'=' * 70}\n>>> 汇总\n{'=' * 70}")
    failed = [p for p in all_results if not p.ok]
    for p in all_results:
        print(f"  {p}")

    if failed:
        print(f"\n{RED}结论：{len(failed)}/{len(all_results)} 项失败，断点如上。{RESET}")
        return 1
    print(f"\n{GREEN}结论：全部 {len(all_results)} 项通过。{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
