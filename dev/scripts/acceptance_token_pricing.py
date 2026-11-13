"""模型单价配置（用户可覆盖）的验收。

覆盖三件事：
  1. 用户文件能覆盖出厂默认，且只覆盖写了的字段；
  2. 非法值/坏文件不崩、不污染配置；
  3. 首次运行放出模板，且已有文件绝不被覆盖。

价格不该写死在源码里：程序不知道用户用哪家供应商、什么套餐，
写死既不准，又会让用户改价就触发「本地修改」而无法更新。
"""
import json
import logging
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_sandbox = tempfile.TemporaryDirectory(prefix="abyss-pricing-")
os.environ["ABYSS_DATA_DIR"] = _sandbox.name

import utils.llm_client_manager as m  # noqa: E402


class TokenPricingAcceptance(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.file = self.tmp / "user_settings" / "token_pricing.json"
        self._original = m.TOKEN_PRICING_FILE
        m.TOKEN_PRICING_FILE = self.file

    def tearDown(self):
        m.TOKEN_PRICING_FILE = self._original
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, document):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps(document), encoding="utf-8")

    def test_user_file_overrides_only_given_fields(self):
        self.write({"deepseek-v4-pro": {"input_per_million": 2.5}})
        pricing = m._load_pricing()
        self.assertEqual(pricing["deepseek-v4-pro"]["input_per_million"], 2.5)
        self.assertEqual(pricing["deepseek-v4-pro"]["output_per_million"], 6)   # 沿用默认
        self.assertEqual(pricing["deepseek-v4-pro"]["cached_per_million"], 0.025)

    def test_user_can_add_model_unknown_to_defaults(self):
        self.write({"my-proxy-model": {"input_per_million": 9, "output_per_million": 9}})
        pricing = m._load_pricing()
        self.assertEqual(pricing["my-proxy-model"]["input_per_million"], 9)
        self.assertEqual(pricing["my-proxy-model"]["cached_per_million"], 0)

    def test_invalid_values_are_ignored_not_fatal(self):
        self.write({
            "deepseek-v4-pro": {"input_per_million": -5, "output_per_million": "abc",
                                "cached_per_million": True},
            "deepseek-v4.1-flash": "不是对象",
        })
        pricing = m._load_pricing()
        pro = pricing["deepseek-v4-pro"]
        self.assertEqual(pro["input_per_million"], 3)        # 出厂默认
        self.assertEqual(pro["output_per_million"], 6)
        self.assertEqual(pro["cached_per_million"], 0.025)
        self.assertEqual(pricing["deepseek-v4.1-flash"]["input_per_million"], 1)

    def test_broken_json_falls_back_to_defaults(self):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text("{ 这不是 json", encoding="utf-8")
        pricing = m._load_pricing()
        self.assertEqual(pricing["deepseek-v4-pro"]["input_per_million"], 3)

    def test_unknown_model_costs_zero(self):
        self.file.unlink(missing_ok=True)
        cost = m._compute_cost("nobody/unknown", completion=1000, cache_hit=0, cache_miss=1000)
        self.assertEqual(cost["cost_total"], 0.0)

    def test_cost_uses_override(self):
        self.write({"glm-5.3-flash": {"input_per_million": 1.0,
                                      "output_per_million": 2.0,
                                      "cached_per_million": 0.5}})
        cost = m._compute_cost("glm-5.3-flash", completion=1_000_000,
                               cache_hit=0, cache_miss=1_000_000)
        self.assertAlmostEqual(cost["cost_total"], 3.0)

    def test_template_released_on_first_run_without_real_prices(self):
        """模板要有格式示例，但不能预设真实模型价（否则用户会当成权威价）。"""
        self.assertFalse(self.file.exists())
        m._load_pricing()
        self.assertTrue(self.file.exists())
        document = json.loads(self.file.read_text(encoding="utf-8"))
        self.assertIn("_说明", document)
        self.assertIn("示例_照抄改数字", document)
        real = [k for k in document if not k.startswith("_") and k != "示例_照抄改数字"]
        self.assertEqual(real, [])

    def test_existing_user_file_is_never_overwritten(self):
        self.write({"deepseek-v4.1-flash": {"input_per_million": 0.3}})
        before = self.file.read_text(encoding="utf-8")
        m._load_pricing()
        self.assertEqual(self.file.read_text(encoding="utf-8"), before)

    def test_defaults_table_exists_and_matches_legacy_keys(self):
        """默认表要保留原有模型名，避免升级后费用忽然全变 0。"""
        for name in ("deepseek-flash", "deepseek-v4.1-flash", "DeepSeek-V4.1-Flash",
                     "deepseek-v4-pro", "glm-5.3-flash", "doubao-embedding-vision"):
            self.assertIn(name, m.TOKEN_PRICING_DEFAULTS)


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    try:
        unittest.main(verbosity=2)
    finally:
        logging.shutdown()
        _sandbox.cleanup()
