#!/usr/bin/env python3
"""Build reviewable Windows release files and independent component packages.

This never modifies the installed 常用版本 (compat link dist/Abyss) or the user's running processes.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from package_component import package

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--components", nargs="+", choices=("launcher", "manager", "core", "ingress"),
                        default=["launcher", "manager", "core", "ingress"])
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "independent-updates")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    versions = json.loads((ROOT / "component_versions.json").read_text(encoding="utf-8"))
    dotnet = str(ROOT / ".tools" / "dotnet" / "dotnet.exe") if (ROOT / ".tools" / "dotnet" / "dotnet.exe").is_file() else "dotnet"
    deps = ROOT / ".tools" / "console-test-deps"
    env = os.environ.copy()
    # The cached dependencies may target a different Python ABI (e.g. cp312).
    if deps.is_dir() and list((deps / "pydantic_core").glob(f"*.cp{sys.version_info.major}{sys.version_info.minor}-*.pyd")):
        env["PYTHONPATH"] = str(deps) + os.pathsep + env.get("PYTHONPATH", "")
    program = output / "Abyss"
    program.mkdir(exist_ok=True)
    for name in args.components:
        if name == "launcher":
            source = output / "components" / "launcher"
            subprocess.run([dotnet, "publish", str(ROOT / "launcher" / "AbyssLauncher.csproj"), "-c", "Release",
                            "-r", "win-x64", "--self-contained", "true", "-p:NuGetAudit=false", "-o", str(source), "--nologo"], check=True, cwd=ROOT)
            helper = source / "updater"
            subprocess.run([dotnet, "publish", str(ROOT / "updater" / "AbyssUpdater.csproj"), "-c", "Release",
                            "-r", "win-x64", "--self-contained", "true", "-p:PublishSingleFile=true",
                            "-p:IncludeNativeLibrariesForSelfExtract=true", "-p:EnableSingleFileAnalyzer=false",
                            "-p:NuGetAudit=false", "-o", str(helper), "--nologo"], check=True, cwd=ROOT)
        else:
            source = output / "components" / name
            subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", str(output / "components"),
                            "--workpath", str(ROOT / "build" / "independent-updates" / "pyinstaller"),
                            str(ROOT / f"{name}.spec")], check=True, cwd=ROOT, env=env)
        # The package policy also prevents stale/private files from being deployed.
        from update_policy import allowed_file
        for item in source.rglob("*"):
            if item.is_file() and allowed_file(name, item.relative_to(source).as_posix()):
                destination = program / item.relative_to(source)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, destination)
        package(name, versions[name], source, output / "packages")
    shutil.copy2(ROOT / "component_versions.json", program / "component_versions.json")
    # Preserve the established repository data root for this local build only.
    (program / "abyss-data-dir.txt").write_text(os.path.relpath(ROOT, program), encoding="utf-8")
    # dotnet/pyinstaller 的中转目录（bin/obj/build）会生成同名 EXE 副本，日常极易误点；
    # 发布完成后一并清掉，磁盘上只留 常用版本（兼容入口 dist/Abyss）与 dist/independent-updates（2026-10-15）。
    for intermediate in (ROOT / "launcher" / "bin", ROOT / "launcher" / "obj",
                         ROOT / "updater" / "bin", ROOT / "updater" / "obj",
                         ROOT / "updater-tests" / "bin", ROOT / "updater-tests" / "obj",
                         ROOT / "build"):
        shutil.rmtree(intermediate, ignore_errors=True)
    print(f"Release files: {program}\nIndependent packages: {output / 'packages'}")


if __name__ == "__main__":
    main()
