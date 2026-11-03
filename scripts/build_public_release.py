"""发布一个启动器 EXE，并生成它按需下载的源码 Core 和独立组件更新包。不会部署到日常运行目录。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from manager_updates import validate_url
from update_policy import allowed_file, COMPONENTS, version_key
from package_component import package
from build_core_source import build, copy_sources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="全新的发布目录")
    parser.add_argument("--feed-base-url", required=True, help="稳定的版本清单地址目录")
    parser.add_argument("--package-base-url", required=True, help="本次版本包的永久下载地址目录")
    parser.add_argument("--metadata", type=Path, required=True, help="本次 Core 的变更与兼容说明 JSON")
    parser.add_argument("--runtime", type=Path, help="可选：复用已经验收的完整 Windows Python 运行环境")
    parser.add_argument("--previous-releases", type=Path, help="后续发布时传入上次 packages 目录，保留历史清单与永久包地址")
    args = parser.parse_args()
    for url in (args.feed_base_url, args.package_base_url):
        validate_url(url)
    output = args.output.resolve()
    if output.exists():
        raise ValueError("发布输出必须是新目录，避免混入过期程序或个人文件")
    output.mkdir(parents=True)
    if args.previous_releases:
        previous = args.previous_releases.resolve()
        if not previous.is_dir():
            raise ValueError("历史发布目录不存在")
        (output / "packages").mkdir()
        for path in previous.glob("*.json"):
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            name, version = value.get("component"), value.get("version")
            if name in COMPONENTS and version and path.name == f"{name}-{version}.json":
                version_key(version)
                validate_url(value["url"])
                shutil.copy2(path, output / "packages" / path.name)
    versions = json.loads((ROOT / "component_versions.json").read_text(encoding="utf-8"))
    metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
    metadata.setdefault("requires", {"manager": versions["manager"], "launcher": versions["launcher"]})
    print("Building source Core with its own Python runtime...", flush=True)
    source_component, _ = build(output, versions["core"], metadata, args.runtime, base_url=args.package_base_url)
    # 管理服务也从脱敏后的模块构建，避免冻结进本机角色或默认允许列表。
    manager_source = output / "build-source"
    copy_sources(manager_source)
    for name in ("manager_service.py", "manager_core.py", "manager_console.py", "manager_updates.py",
                 "core_update_review.py", "update_policy.py", "manager.spec", "component_versions.json"):
        shutil.copy2(ROOT / name, manager_source / name)
    (manager_source / "assets").mkdir()
    shutil.copy2(ROOT / "assets/icon.ico", manager_source / "assets/icon.ico")
    print("Building resident manager...", flush=True)
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", str(output / "components"),
                    "--workpath", str(output / "build-manager"), "manager.spec"], cwd=manager_source, check=True)
    manager = output / "components/manager"
    package("manager", versions["manager"], manager, output / "packages", args.package_base_url,
            notes="Core 手动确认、源码组件安装、本地修改保护与更新备份。")
    dotnet = str(ROOT / ".tools" / "dotnet" / "dotnet.exe") if (ROOT / ".tools" / "dotnet" / "dotnet.exe").is_file() else "dotnet"
    helper = output / "updater"
    subprocess.run([dotnet, "publish", str(ROOT / "updater/AbyssUpdater.csproj"), "-c", "Release", "-r", "win-x64",
                    "--self-contained", "true", "-p:PublishSingleFile=true", "-p:IncludeNativeLibrariesForSelfExtract=true",
                    "-p:NuGetAudit=false", "-o", str(helper), "--nologo"], check=True, cwd=ROOT)
    defaults = {"auto_check": True, "core_distribution": "source-v1", "sources": {
        name: args.feed_base_url.rstrip("/") + f"/{name}-latest.json" for name in ("core", "manager", "launcher")}}
    defaults_path = output / "update_defaults.json"
    defaults_path.write_text(json.dumps(defaults, indent=2), encoding="utf-8")
    bundle = output / "bootstrap.zip"
    entries = [(p, p.relative_to(manager).as_posix()) for p in manager.rglob("*") if p.is_file()
               and allowed_file("manager", p.relative_to(manager).as_posix())]
    entries += [(helper / "AbyssUpdater.exe", "updater/AbyssUpdater.exe"),
                (ROOT / "component_versions.json", "component_versions.json"), (defaults_path, "update_defaults.json")]
    manifest = {}
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, name in entries:
            with path.open("rb") as stream:
                manifest[name] = hashlib.file_digest(stream, "sha256").hexdigest()
            archive.write(path, name)
        archive.writestr("bootstrap.manifest.json", json.dumps(manifest))
    print("Building the single public launcher EXE...", flush=True)
    launcher = output / "components/launcher"
    subprocess.run([str(dotnet), "publish", str(ROOT / "launcher/AbyssLauncher.csproj"), "-c", "Release", "-r", "win-x64",
                    "--self-contained", "true", "-p:PublishSingleFile=true", "-p:IncludeNativeLibrariesForSelfExtract=true",
                    "-p:NuGetAudit=false", "-p:BootstrapBundle=" + str(bundle), "-o", str(launcher), "--nologo"], check=True, cwd=ROOT)
    (launcher / "updater").mkdir(exist_ok=True)
    shutil.copy2(helper / "AbyssUpdater.exe", launcher / "updater/AbyssUpdater.exe")
    package("launcher", versions["launcher"], launcher, output / "packages", args.package_base_url,
            notes="单 EXE 首次安装；Core 下载与安装分别确认。")
    public = output / "download"
    public.mkdir()
    shutil.copy2(launcher / "AbyssLauncher.exe", public / "AbyssLauncher.exe")
    (output / "release-summary.json").write_text(json.dumps({"versions": versions, "feed_base_url": args.feed_base_url,
        "package_base_url": args.package_base_url, "source_component": str(source_component),
        "entry": str(public / "AbyssLauncher.exe"), "core_auto_download": False, "core_auto_install": False}, indent=2), encoding="utf-8")
    print(f"Public entry: {public / 'AbyssLauncher.exe'}\nPublish packages and JSON feeds from: {output / 'packages'}", flush=True)


if __name__ == "__main__":
    main()
