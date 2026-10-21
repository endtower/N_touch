"""接入配置读写验收：真实 HTTP + 临时数据目录，不启动 core / snowluma / LLM。

覆盖三条契约（manager_service 的 /v1/config/core，经 /system/config 复用）：
1. GET 掩码：密钥类字段只回 {KEY}_SET，不回传明文；
2. 脏字段保存：PUT 只发用户触碰过的字段，未进 payload 的密钥不被清空；
3. 注入防护：自由文本字段拒绝换行/控制符，非法值一律 400 且不写盘。
另附端口规范化、新键追加与未知字段拒绝，以及已保存配置解析预览
（effective：各任务 base_url / provider / 模型名 / key 是否设置，密钥只回布尔位）。
"""

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

INITIAL_ENV = "\n".join([
    "FLASH_API_KEY=secret-flash",
    "FLASH_BASE_URL=https://ark.cn-beijing.volces.com/api/coding/v3",
    "PERSONA_API_KEY=secret-persona",
    "WEB_WS_PORT=5002",
    "LLM_FLASH=deepseek-v4.1-flash",
    "",
])


class ConfigApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"ABYSS_DATA_DIR": str(self.root)})
        self.env.start()
        (self.root / ".env.dev").write_text(INITIAL_ENV, encoding="utf-8")
        # ABYSS_DATA_DIR 生效后再导入：令牌/配置路径都钉到临时目录
        self.service = importlib.import_module("manager_service")
        app = web.Application()
        app.router.add_get("/v1/config/core", self.service.handle_config_get)
        app.router.add_put("/v1/config/core", self.service.handle_config_put)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.headers = {"X-Manager-Token": self.service.TOKEN}

    async def asyncTearDown(self):
        await self.client.close()
        os.chdir(ROOT)  # manager_service 导入时 ensure_cwd() 改过工作目录
        self.env.stop()
        self.tmp.cleanup()

    def env_map(self):
        result = {}
        for line in (self.root / ".env.dev").read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, _, value = line.partition("=")
                result[key.strip()] = value.strip()
        return result

    async def test_unauthorized_request_is_denied(self):
        self.assertEqual((await self.client.get("/v1/config/core")).status, 401)

    async def test_get_masks_secrets_and_reports_set_flags(self):
        data = await (await self.client.get("/v1/config/core", headers=self.headers)).json()
        fields = data["fields"]
        self.assertTrue(fields["FLASH_API_KEY_SET"])
        self.assertNotIn("FLASH_API_KEY", fields)               # 明文不回传
        self.assertFalse(fields["CONVERSATION_API_KEY_SET"])
        self.assertFalse(fields["VISION_API_KEY_SET"])
        self.assertFalse(fields["VECTOR_API_KEY_SET"])
        self.assertEqual(fields["LLM_FLASH"], "deepseek-v4.1-flash")
        self.assertEqual(fields["WEB_WS_PORT"], 5002)
        self.assertNotIn("secret-flash", str(fields))

    async def test_effective_snapshot_reports_saved_values_without_secrets(self):
        data = await (await self.client.get("/v1/config/core", headers=self.headers)).json()
        effective = data["effective"]
        # common：FLASH_BASE_URL 已落盘 → env 来源；模型名走 .env.dev 的 LLM_FLASH
        common = effective["common"]
        self.assertEqual(common["base_url"], "https://ark.cn-beijing.volces.com/api/coding/v3")
        self.assertEqual(common["provider"], "火山方舟")
        self.assertEqual(common["model"], "deepseek-v4.1-flash")
        self.assertTrue(common["key_set"])
        self.assertEqual(common["base_url_source"], "env")
        # conversation / vision：未配置专属接入点 → 整体回退 common
        for name in ("conversation", "vision"):
            entry = effective[name]
            self.assertEqual(entry["base_url"], common["base_url"], name)
            self.assertEqual(entry["key_source"], "fallback", name)
        # embed：VECTOR 未配置 → 回退 FLASH 接入点，内置 key 明文永不出现
        embed = effective["embed"]
        self.assertEqual(embed["base_url"], common["base_url"])
        self.assertEqual(embed["model"], "doubao-embedding-vision")
        # 快照任何位置都不得泄漏密钥明文
        self.assertNotIn("secret-flash", str(effective))
        self.assertNotIn("secret-persona", str(effective))

    async def test_dirty_only_put_keeps_untouched_secrets(self):
        response = await self.client.put("/v1/config/core", headers=self.headers,
                                         json={"fields": {"LLM_FLASH": "deepseek-v4-flash"}})
        self.assertEqual(response.status, 200)
        env = self.env_map()
        self.assertEqual(env["LLM_FLASH"], "deepseek-v4-flash")
        # 未提交的密钥原样保留（前端「留空不改」契约）
        self.assertEqual(env["FLASH_API_KEY"], "secret-flash")
        self.assertEqual(env["PERSONA_API_KEY"], "secret-persona")

    async def test_first_run_identity_fields_are_saved_without_touching_keys(self):
        fields = {"MASTER_ID": "123456", "MASTER_NAME": "测试 用户", "ASSISTANT_QQ": "654321", "ASSISTANT_NAME": "测试 助手"}
        response = await self.client.put("/v1/config/core", headers=self.headers, json={"fields": fields})
        self.assertEqual(response.status, 200, await response.text())
        saved = self.env_map()
        for key, value in fields.items():
            self.assertEqual(saved[key], value)
        self.assertEqual(saved["FLASH_API_KEY"], "secret-flash")

    async def test_only_common_and_embed_are_needed_and_all_optional_models_inherit(self):
        from config_keys import CONFIG_OVERRIDE_KEYS
        config = {
            "FLASH_API_KEY": "common-test-key", "FLASH_BASE_URL": "https://common.example/v1",
            "LLM_FLASH": "common-custom-model", "VECTOR_API_KEY": "embed-test-key",
            "VECTOR_BASE_URL": "https://embed.example/v1", "LLM_EMBEDDING": "embed-custom-model",
        }
        with patch.dict(os.environ, {key: "" for key in CONFIG_OVERRIDE_KEYS}):
            snapshot = self.service._effective_snapshot(config)
            for name in ("conversation", "persona", "vision", "pro"):
                self.assertEqual(snapshot[name]["base_url"], config["FLASH_BASE_URL"])
                self.assertEqual(snapshot[name]["model"], config["LLM_FLASH"])
                self.assertTrue(snapshot[name]["key_set"])
                self.assertEqual(snapshot[name]["model_source"], "fallback")
            self.assertEqual(snapshot["embed"]["model"], "embed-custom-model")
            self.assertEqual(snapshot["embed"]["base_url"], "https://embed.example/v1")
            self.assertNotIn("common-test-key", str(snapshot))
            self.assertNotIn("embed-test-key", str(snapshot))

    async def test_partial_overrides_inherit_each_missing_common_field(self):
        from config_keys import CONFIG_OVERRIDE_KEYS
        with patch.dict(os.environ, {key: "" for key in CONFIG_OVERRIDE_KEYS}):
            for prefix, name in (("CONVERSATION", "conversation"), ("PERSONA", "persona"), ("VISION", "vision")):
                entry = self.service._effective_snapshot({
                    "FLASH_API_KEY": "common-key", "FLASH_BASE_URL": "https://common.example/v1",
                    "LLM_FLASH": "common-model", f"{prefix}_API_KEY": "own-key",
                })[name]
                self.assertEqual(entry["base_url"], "https://common.example/v1")
                self.assertEqual(entry["model"], "common-model")
                self.assertEqual(entry["key_source"], "env")
                entry = self.service._effective_snapshot({
                    "FLASH_API_KEY": "common-key", "FLASH_BASE_URL": "https://common.example/v1",
                    "LLM_FLASH": "common-model", f"{prefix}_BASE_URL": "https://own.example/v1",
                })[name]
                self.assertEqual(entry["base_url"], "https://own.example/v1")
                self.assertTrue(entry["key_set"])
                self.assertEqual(entry["key_source"], "fallback")

    async def test_saved_preview_uses_new_model_and_explicit_clear_despite_stale_environment(self):
        with patch.dict(os.environ, {"LLM_FLASH": "old-model", "LLM_PERSONA": "old-persona"}):
            response = await self.client.put("/v1/config/core", headers=self.headers, json={"fields": {
                "LLM_FLASH": "new-common", "LLM_PERSONA": "", "PERSONA_API_KEY": "",
                "LLM_PRO": "custom-pro",
            }})
            self.assertEqual(response.status, 200)
            body = await response.json()
            self.assertTrue(body["requires_restart"])
            for result in (body, await (await self.client.get("/v1/config/core", headers=self.headers)).json()):
                self.assertEqual(result["effective_scope"], "saved")
                self.assertEqual(result["effective"]["common"]["model"], "new-common")
                self.assertEqual(result["effective"]["persona"]["model"], "new-common")
                self.assertEqual(result["effective"]["pro"]["model"], "custom-pro")

    async def test_no_config_does_not_claim_embedded_credentials_are_ready(self):
        from config_keys import CONFIG_OVERRIDE_KEYS
        with patch.dict(os.environ, {key: "" for key in CONFIG_OVERRIDE_KEYS}):
            snapshot = self.service._effective_snapshot({})
            self.assertFalse(snapshot["common"]["key_set"])
            self.assertFalse(snapshot["embed"]["key_set"])

    async def test_explicit_clear_writes_empty_value(self):
        response = await self.client.put("/v1/config/core", headers=self.headers,
                                         json={"fields": {"PERSONA_API_KEY": ""}})
        self.assertEqual(response.status, 200)
        env = self.env_map()
        self.assertEqual(env["PERSONA_API_KEY"], "")
        self.assertEqual(env["FLASH_API_KEY"], "secret-flash")

    async def test_new_keys_are_appended_and_port_normalized(self):
        response = await self.client.put("/v1/config/core", headers=self.headers,
                                         json={"fields": {"VISION_API_KEY": "vk-1", "WEB_WS_PORT": "5007"}})
        self.assertEqual(response.status, 200)
        env = self.env_map()
        self.assertEqual(env["VISION_API_KEY"], "vk-1")
        self.assertEqual(env["WEB_WS_PORT"], "5007")
        self.assertEqual(env["LLM_FLASH"], "deepseek-v4.1-flash")   # 原有键不动

    async def test_injection_and_bad_values_are_rejected_without_writing(self):
        before = (self.root / ".env.dev").read_bytes()
        for payload in (
            {"ONEBOT_ACCESS_TOKEN": "abc\ndef"},
            {"VISION_BASE_URL": "https://evil\nX=1"},
            {"FLASH_API_KEY": "a b"},
            {"FLASH_BASE_URL": "ftp://x"},
            {"ONEBOT_WS_PATH": "no-slash"},
            {"LLM_FLASH": "bad model!"},
            {"WEB_WS_PORT": 70000},
            {"WEB_WS_PORT": "not-a-port"},
        ):
            response = await self.client.put("/v1/config/core", headers=self.headers, json={"fields": payload})
            self.assertEqual(response.status, 400, payload)
        self.assertEqual((self.root / ".env.dev").read_bytes(), before)

    async def test_unknown_field_and_bad_shapes_are_rejected(self):
        for body in ({"fields": {"NOT_A_KEY": "x"}}, {"fields": ["FLASH_API_KEY"]}, {"fields": "x"}):
            response = await self.client.put("/v1/config/core", headers=self.headers, json=body)
            self.assertEqual(response.status, 400, body)


if __name__ == "__main__":
    unittest.main()
