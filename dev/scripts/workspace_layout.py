"""Shared development locations; build/test output never belongs in source or data."""
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
WORKSPACE = SOURCE.parent if SOURCE.name == "dev" else SOURCE
APP = WORKSPACE / "app"
DATA = WORKSPACE / "data"
CACHE = WORKSPACE / "cache"
BUILDS = CACHE / "builds"
TOOLS = CACHE / "tools"
TEST_ROOT = CACHE / "tests"


def test_root() -> Path:
    TEST_ROOT.mkdir(parents=True, exist_ok=True)
    return TEST_ROOT
