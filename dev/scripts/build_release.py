"""Build the source-only layout; all business components share this release route."""
import argparse
from pathlib import Path
from build_source_distribution import build, ROOT
from workspace_layout import BUILDS

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=BUILDS / "independent-updates")
    parser.add_argument("--components", nargs="+", choices=("launcher", "manager", "core", "ingress"))
    parser.add_argument("--private", action="store_true", help="本机迁移：保留个人 Python 设置，不生成公共包")
    args = parser.parse_args()
    build(args.output, private=args.private)
