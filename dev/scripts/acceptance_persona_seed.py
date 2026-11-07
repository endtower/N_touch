"""人格播种验收：全新数据目录（无人格文件）自动播种空 common，且不放松坏文件契约。

五条断言：
1. 空目录 → 自动播种且只播种一个 common.json（正文留空给用户写）；
2. 空目录 → DEFAULT_PERSONA_MODE 回落到 common，取默认人格不再 KeyError；
3. 已有 common + normal → 不播种、DEFAULT 仍为 normal、原文件字节不变；
4. 空目录 → 可入参人格（selectable）含 common，内荐清单仍排除它；
5. 文件已存在但内容坏（缺字段 / 缺基本人格）→ 照旧崩溃阻断。
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
REGISTRY = ROOT / "brain" / "base" / "persona" / "persona_registry.py"

# 预热日志：此时 ABYSS_DATA_DIR 未设置，日志文件句柄落在真实 logs/ 下。
# 否则注册表导入时会在临时数据目录里开句柄，导致临时目录清理失败（WinError 32）。
import utils.logger  # noqa: E402

utils.logger.get_logger("persona_registry", "INFO")


def _load_registry(root: Path, name: str):
    """在隔离的 ABYSS_DATA_DIR 下，以唯一模块名重新执行注册表模块（重跑加载逻辑）。"""
    os.environ["ABYSS_DATA_DIR"] = str(root)
    spec = importlib.util.spec_from_file_location(name, REGISTRY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_persona(directory: Path, name: str, **fields) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    path.write_text(json.dumps({"persona_name": name, **fields}, ensure_ascii=False), encoding="utf-8")
    return path


class PersonaSeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dir = self.root / "brain" / "base" / "persona"
        self.addCleanup(os.environ.pop, "ABYSS_DATA_DIR", None)
        self.addCleanup(self.tmp.cleanup)

    def test_empty_dir_seeds_single_empty_common(self):
        module = _load_registry(self.root, "persona_seed_empty")
        self.assertEqual(sorted(p.name for p in self.dir.glob("*.json")), ["common.json"])
        base = module.get_base_persona()
        self.assertEqual(base.persona_name, "common")
        self.assertEqual(base.kernel_prompt, "")      # 人格信息默认没有，用户自己写
        self.assertTrue(base.scene)                   # 场景给占位，供人格清单展示
        self.assertEqual(module.DEFAULT_PERSONA_MODE, "common")
        module.get_persona(module.DEFAULT_PERSONA_MODE)  # 默认人格可取，不再 KeyError

    def test_existing_personas_untouched(self):
        _write_persona(self.dir, "common", scene="验收", kernel_prompt="正文")
        _write_persona(self.dir, "normal", scene="验收", kernel_prompt="正文")
        before = (self.dir / "common.json").read_bytes()
        module = _load_registry(self.root, "persona_seed_existing")
        self.assertEqual(module.DEFAULT_PERSONA_MODE, "normal")
        self.assertEqual(len(list(self.dir.glob("*.json"))), 2)   # 未额外播种
        self.assertEqual((self.dir / "common.json").read_bytes(), before)

    def test_empty_dir_selectable_contains_common(self):
        """全新安装只有 common：它既是默认人格，也必须能通过工具入参校验。"""
        module = _load_registry(self.root, "persona_seed_selectable")
        selectable = [p.persona_name for p in module.list_selectable_personas()]
        self.assertEqual(selectable, ["common"])                  # 校验范围含基本人格
        self.assertEqual(module.list_roster_personas(), [])       # 内荐清单仍排除它
        roster_text = module.render_persona_roster()
        self.assertNotIn("你具有以下人格。", roster_text)          # 不输出空壳标题
        self.assertIn("common", roster_text)                      # 明确说明只有基本人格

    def test_broken_file_still_crashes(self):
        _write_persona(self.dir, "common")                 # 缺 scene / kernel_prompt
        with self.assertRaises(KeyError):
            _load_registry(self.root, "persona_seed_broken")

    def test_missing_common_still_crashes(self):
        _write_persona(self.dir, "normal", scene="验收", kernel_prompt="正文")
        with self.assertRaises(RuntimeError):              # 有文件但缺基本人格
            _load_registry(self.root, "persona_seed_no_common")


if __name__ == "__main__":
    unittest.main()
