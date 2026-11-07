"""构建可直接运行的 Core 源码组件；不复制本机配置、人格、记忆或登录数据。"""
from __future__ import annotations

import argparse
import ast
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core_update_review import release_details
from update_policy import core_source_file
from package_component import package
from workspace_layout import TOOLS


ROLE_TEMPLATE = '''from config.env_tools import env_str
class RoleConfig:
    ASSISTANT_NAME = env_str("ASSISTANT_NAME", "助手")
    ASSISTANT_NICK_NAME = env_str("ASSISTANT_NICK_NAME", ASSISTANT_NAME)
    ASSISTANT_ID = "system"
    ASSISTANT_NAME_DESCRIPTION = ASSISTANT_NAME
    MASTER_NAME = env_str("MASTER_NAME", "主人")
    MASTER_ID = env_str("MASTER_ID", "0")
    ASSISTANT_QQ = env_str("ASSISTANT_QQ", "0")
role_config = RoleConfig()
'''

INITIALIZER_TEMPLATE = '''"""全新安装只建立必要身份，不导入开发者的群、经验或个人印象。"""
from datetime import datetime
class DefaultInitializer:
    @staticmethod
    async def initialize_all():
        from brain.hc.database.core import get_memory_db
        from brain.hc.database import entity_identity
        from config.role_settings import role_config
        conn = await get_memory_db()._ensure_connection()
        now = datetime.now().isoformat()
        identities = [("00000000", role_config.ASSISTANT_NAME, "")]
        if role_config.MASTER_ID not in ("", "0"):
            identities.append(("00000001", role_config.MASTER_NAME, "private_" + role_config.MASTER_ID))
        for uuid, name, session_id in identities:
            await conn.execute("INSERT OR IGNORE INTO characters (uuid,usage_count,last_used_at,created_at) VALUES (?,0,?,?)", (uuid, now, now))
            await entity_identity.set_primary_name(conn, entity_identity.CHARACTER, uuid, name)
            if session_id:
                await entity_identity.add_contact(conn, entity_identity.CHARACTER, uuid, session_id)
        await conn.commit()
'''


def copy_sources(destination: Path, private: bool = False):
    names = [p for directory in ("brain", "adapt", "core", "utils", "memory", "config")
             for p in (ROOT / directory).rglob("*.py") if "tests" not in p.parts]
    names += [ROOT / name for name in ("main.py", "ingress_service.py", "ingress_config.py", "paths.py",
                                      "config_keys.py", "truck_settings_store.py", "user_prompt_settings.py", "console_context.py", "console_history.py")]
    for src in names:
        relative = src.relative_to(ROOT).as_posix()
        if not core_source_file("components/core/source/" + relative):
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if private:
            shutil.copy2(src, target)
        elif relative in ("config/role_settings.py", "brain/hc/database/default_initializer.py"):
            target.write_text(ROLE_TEMPLATE if relative.startswith("config/") else INITIALIZER_TEMPLATE, encoding="utf-8")
        elif relative == "config/settings.py":
            tree = ast.parse(src.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name == "SafetyConfig":
                    for entry in node.body:
                        if isinstance(entry, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "CONVERSATION_ALLOWED_SESSIONS" for t in entry.targets):
                            entry.value = ast.List(elts=[], ctx=ast.Load())
            # 公开默认实现不包含本机注释中的账号或个性化允许列表。
            target.write_text(ast.unparse(tree) + "\n", encoding="utf-8")
        else:
            shutil.copy2(src, target)
    for name in ("requirements.txt", ".env.example"):
        shutil.copy2(ROOT / name, destination / name)
    shutil.copy2(ROOT / "docs/STORAGE.md", destination / "STORAGE.md")
    # 发布必需的默认模块可独立跟踪；本机被忽略的个性化配置不进入公共源码包。
    for path in (() if private else (ROOT / "release_defaults").rglob("*.py")):
        target = destination / path.relative_to(ROOT / "release_defaults")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    frontend = ROOT / "web_frontend/dist"
    if not (frontend / "index.html").is_file():
        raise ValueError("请先构建 Web UI；源码 Core 必须携带与后端匹配的前端")
    shutil.copytree(frontend, destination / "web_frontend/dist")
    for directory in ("src", "public"):
        if (ROOT / "web_frontend" / directory).is_dir():
            shutil.copytree(ROOT / "web_frontend" / directory, destination / "web_frontend" / directory)
    for path in (ROOT / "web_frontend").iterdir():
        if path.is_file() and (path.name in ("package.json", "package-lock.json", "index.html", "vite.config.ts") or path.name.startswith("tsconfig") and path.suffix == ".json"):
            shutil.copy2(path, destination / "web_frontend" / path.name)


def _copy_package_without_record(dist, site: Path, name: str) -> None:
    """手工放置的依赖常缺 RECORD（dist.files 为 None），按实际导入位置整体复制。

    不能静默跳过：那会让发布的 runtime 少依赖，用户端首次用到才报
    ModuleNotFoundError（例如向量比对缺 numpy / aiosqlite）。这里复制完整
    包目录与 dist-info，并在构建输出里明确提示。
    """
    top = name.lower().replace("-", "_")
    spec = importlib.util.find_spec(top)
    if spec is None or (not spec.submodule_search_locations and not spec.origin):
        raise ValueError(f"依赖 {name} 缺文件清单且无法定位导入位置，请重新安装 requirements.txt")
    if spec.submodule_search_locations:
        source = Path(list(spec.submodule_search_locations)[0])
    else:
        source = Path(spec.origin)
    if not source.exists():
        raise ValueError(f"依赖 {name} 的包内容不存在: {source}")
    target = site / source.name
    if source.is_dir():
        shutil.copytree(source, target, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests"))
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    info = Path(str(getattr(dist, "_path", "")))
    if info.is_dir():
        shutil.copytree(info, site / info.name, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    print(f"依赖 {name} 缺少 RECORD，已按包目录整体复制: {source}", flush=True)


def copy_runtime(destination: Path, reuse_standard_library=False):
    """使用本次构建解释器及已安装依赖；把解析到的准确版本写入锁文件。"""
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.markers import default_environment
    base = Path(sys.base_prefix)
    if sys.platform != "win32" or not (base / "python.exe").is_file():
        raise ValueError("源码组件的受管运行环境须在 Windows Python 下构建")
    destination.mkdir(parents=True, exist_ok=reuse_standard_library)
    for pattern in ("python*.exe", "python*.dll", "vcruntime*.dll", "LICENSE.txt"):
        for path in base.glob(pattern):
            shutil.copy2(path, destination / path.name)
    if not reuse_standard_library:
        for directory in ("DLLs", "Lib"):
            shutil.copytree(base / directory, destination / directory,
                            ignore=shutil.ignore_patterns("site-packages", "__pycache__", "test", "tests", "idlelib", "tkinter", "turtledemo", "ensurepip"))
    environment = default_environment() | {"extra": ""}
    pending = [Requirement(line.split("#", 1)[0].strip()) for line in (ROOT / "requirements.txt").read_text(encoding="utf-8-sig").splitlines()
               if line.strip() and not line.lstrip().startswith("#") and not line.lower().startswith("pyinstaller")]
    found = {}
    site = destination / "Lib/site-packages"
    site.mkdir(parents=True, exist_ok=True)
    while pending:
        requirement = pending.pop()
        if requirement.marker and not requirement.marker.evaluate(environment):
            continue
        key = requirement.name.lower().replace("_", "-")
        try:
            dist = importlib.metadata.distribution(requirement.name)
        except importlib.metadata.PackageNotFoundError:
            # 本机有手动复制的纯 Python 包时，从构建缓存取完整元数据，仍严格检查二进制 ABI。
            key_name = requirement.name.lower().replace("_", "-")
            dist = next((d for d in importlib.metadata.distributions(path=[str(TOOLS / "console-test-deps")])
                         if d.metadata.get("Name", "").lower().replace("_", "-") == key_name), None)
            if dist is None:
                raise ValueError(f"构建环境缺少依赖 {requirement.name}，请先安装 requirements.txt")
        if requirement.specifier and dist.version not in requirement.specifier:
            raise ValueError(f"构建依赖不满足要求: {requirement}")
        if key in found:
            continue
        found[key] = dist.version
        if dist.files is None:
            _copy_package_without_record(dist, site, requirement.name)
        for entry in dist.files or []:
            relative = Path(str(entry))
            if relative.is_absolute() or ".." in relative.parts or "__pycache__" in relative.parts or relative.suffix == ".pyc":
                continue
            source = Path(dist.locate_file(entry))
            if source.is_file():
                if source.suffix == ".pyd" and ".cp" in source.name and f".cp{sys.version_info.major}{sys.version_info.minor}-" not in source.name:
                    raise ValueError(f"依赖二进制与构建 Python 不匹配: {source.name}")
                if source.is_symlink() or source.is_junction():
                    raise ValueError("依赖包包含链接")
                target = site / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        pending += [Requirement(item) for item in dist.requires or []]
    (destination / f"python{sys.version_info.major}{sys.version_info.minor}._pth").write_text(
        ".\nLib\nDLLs\nLib/site-packages\n../source\nimport site\n", encoding="utf-8")
    return "\n".join(f"{name}=={version}" for name, version in sorted(found.items())) + "\n"


def build(output: Path, version: str, metadata: dict, runtime: Path | None = None, base_url=""):
    details = release_details(metadata | {"format": "source-v1"})
    source = output / "components" / "core-source"
    if source.exists():
        raise ValueError("源码构建目录已存在，请使用新的输出目录，避免混入旧文件")
    bundle = source / "components/core"
    bundle.mkdir(parents=True)
    copy_sources(bundle / "source")
    if runtime:
        if not (runtime / "python.exe").is_file():
            raise ValueError("运行环境缺少 python.exe")
        shutil.copytree(runtime, bundle / "runtime")
        if (runtime / "requirements.lock").is_file():
            shutil.copy2(runtime / "requirements.lock", bundle / "source/requirements.lock")
    else:
        locked = copy_runtime(bundle / "runtime")
        (bundle / "source/requirements.lock").write_text(locked, encoding="utf-8")
    (bundle / "release.json").write_text(json.dumps({"version": version, **details}, ensure_ascii=False, indent=2), encoding="utf-8")
    archive = package("core", version, source, output / "packages", base_url, metadata=details)
    return source, archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--base-url", default="")
    args = parser.parse_args()
    print(build(args.output.resolve(), args.version, json.loads(args.metadata.read_text(encoding="utf-8")), args.runtime, args.base_url)[1])


if __name__ == "__main__":
    main()
