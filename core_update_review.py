"""Core 更新的发布说明、兼容检查及源码改动检查；不执行安装。"""
from __future__ import annotations

from pathlib import Path
import hashlib
import os

from update_policy import COMPONENTS, CORE_SOURCE, allowed_file, version_key


def release_details(value: dict) -> dict:
    package_format = value.get("format", "binary")
    if package_format not in ("binary", "source-v1"):
        raise ValueError("未知的 Core 程序包格式")
    notes = value.get("notes", "")
    changes = value.get("breaking_changes", [])
    requires = value.get("requires", {})
    data_policy = value.get("data_policy", "unknown")
    migration = value.get("migration_notes", "")
    if not isinstance(notes, str) or len(notes) > 20000:
        raise ValueError("版本说明格式无效")
    if (not isinstance(changes, list) or len(changes) > 100
            or any(not isinstance(x, str) or len(x) > 4000 for x in changes)):
        raise ValueError("破坏性变更说明格式无效")
    if not isinstance(requires, dict) or set(requires) - set(COMPONENTS):
        raise ValueError("组件兼容要求格式无效")
    for minimum in requires.values():
        version_key(minimum)
    if data_policy not in ("compatible", "migration", "unknown") or not isinstance(migration, str) or len(migration) > 20000:
        raise ValueError("数据迁移说明格式无效")
    if data_policy == "migration" and not migration.strip():
        raise ValueError("涉及数据迁移的 Core 必须提供迁移与恢复说明")
    return {"format": package_format, "notes": notes, "breaking_changes": changes,
            "requires": requires, "data_policy": data_policy, "migration_notes": migration}


def file_hash(path: Path) -> str:
    if path.is_symlink() or path.is_junction():
        raise ValueError("程序文件包含链接，请先处理本地修改")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def installed_files(root: Path) -> dict[str, str]:
    """仅读取 Core 拥有的程序，不扫描数据库、聊天、个人配置。"""
    files = {}
    candidates = [root / "core.exe"]
    for directory in (root / "core_internal", root / "components" / "core"):
        if directory.is_symlink() or directory.is_junction():
            raise ValueError("程序目录包含链接，拒绝检查或覆盖")
        if directory.is_dir():
            for parent, directories, filenames in os.walk(directory, followlinks=False):
                for name in directories:
                    child = Path(parent) / name
                    if child.is_symlink() or child.is_junction():
                        raise ValueError("程序目录包含链接，默认保留现有文件")
                directories[:] = [x for x in directories if x not in ("__pycache__", ".git", "node_modules")]
                candidates.extend(Path(parent) / name for name in filenames)
    for path in candidates:
        name = path.relative_to(root).as_posix()
        if path.is_file() and allowed_file("core", name):
            files[name] = file_hash(path)
    return files


def local_changes(receipt: dict, actual: dict[str, str], target: Path) -> list[str]:
    baseline = receipt.get("files", {})
    if not baseline or receipt.get("target_root") != str(target.resolve()):
        return ["源码组件缺少官方安装基线，默认保留现有源码"] if any(p.startswith(CORE_SOURCE) for p in actual) else []
    changed = [name for name, digest in baseline.items()
               if allowed_file("core", name) and actual.get(name) != digest]
    changed.extend(name for name in actual if name not in baseline)
    return sorted(changed)
