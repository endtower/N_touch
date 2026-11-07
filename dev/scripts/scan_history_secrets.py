"""全历史敏感信息扫描：只读遍历所有提交，不修改任何内容。

目的：仓库从私有转公开，历史提交会一并暴露。逐提交检查
1) 疑似密钥字面量（sk- / Bearer / 长 hex / api_key=...）；
2) 个人身份信息（手机号、邮箱、QQ/群号、内网 IP）；
并对命中按「路径 + 提交数」汇总，便于人工判断。

用法：python scripts/scan_history_secrets.py [--max-commits N]
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import defaultdict

# 只看这些形态；宁可多报，勿漏报。
PATTERNS = {
    "openai_style_key": re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),
    "bearer_token": re.compile(r"Bearer\s+[A-Za-z0-9._\-]{24,}"),
    "long_hex": re.compile(r"\b[0-9a-f]{40,}\b"),
    "api_key_assign": re.compile(
        r"(?i)\b(api[_-]?key|secret|access[_-]?token|password)\b\s*[=:]\s*[\"'][^\"'\s]{12,}[\"']"
    ),
    "email": re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
    "cn_mobile": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "private_ip": re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b"),
    "qq_group": re.compile(r"\b(?:private|group|wechat_private)_\d{6,}\b"),
}

# 已知的无害命中：示例文档、测试替身、公开账号地址。
ALLOW = (
    "synthetic-test-token",
    "example.com",
    "users.noreply.github.com",
    "@vitejs",
    "@types/",
    "@vue/",
    "@element-plus/",
    "@rollup/",
    "@babel/",
    "test-token-123",
)


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-commits", type=int, default=0, help="0 表示全部")
    args = parser.parse_args()

    revs = git("rev-list", "--all").split()
    if args.max_commits:
        revs = revs[: args.max_commits]
    print(f"扫描 {len(revs)} 个提交（全部历史）…", flush=True)

    hits: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    for index, rev in enumerate(revs, 1):
        if index % 20 == 0:
            print(f"  已检查 {index}/{len(revs)}", flush=True)
        # -I: 跳过二进制；逐行带文件名输出
        out = git("grep", "-I", "-n", "-E",
                  "|".join(p.pattern for p in PATTERNS.values()), rev, "--")
        for line in out.splitlines():
            parts = line.split(":", 3)
            if len(parts) < 4:
                continue
            _, path, lineno, text = parts
            for name, pattern in PATTERNS.items():
                match = pattern.search(text)
                if not match:
                    continue
                value = match.group(0)
                if any(allowed in value or allowed in path for allowed in ALLOW):
                    continue
                hits[f"{name}"][path].add(f"{value} (L{lineno})")

    if not hits:
        print("\n未发现可疑内容。", flush=True)
        return 0

    print("\n=== 命中汇总（按类别） ===", flush=True)
    for name in sorted(hits):
        print(f"\n[{name}] 影响 {len(hits[name])} 个文件", flush=True)
        for path in sorted(hits[name]):
            samples = sorted(hits[name][path])[:3]
            print(f"  {path}  ({len(hits[name][path])} 处)", flush=True)
            for sample in samples:
                print(f"      {sample}", flush=True)
    print("\n提示：确认后可用 git filter-repo 清理历史，或在公开前重建仓库。", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
