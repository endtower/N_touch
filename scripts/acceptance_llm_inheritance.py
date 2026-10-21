"""实际模型注册表与 client 路由验收；临时配置、模拟 client，不调用 LLM。"""
import os
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from config_keys import CONFIG_OVERRIDE_KEYS

DATA = tempfile.TemporaryDirectory(prefix="abyss_llm_inheritance_")
ENV = patch.dict(os.environ, {**{key: "" for key in CONFIG_OVERRIDE_KEYS}, "ABYSS_DATA_DIR": DATA.name,
                            "FLASH_API_KEY": "common-test-key", "FLASH_BASE_URL": "https://common.example/v1",
                            "LLM_FLASH": "custom-common", "VECTOR_API_KEY": "embed-test-key",
                            "VECTOR_BASE_URL": "https://embed.example/v1", "LLM_EMBEDDING": "custom-embed"})
ENV.start()
Path(DATA.name, ".env.dev").touch()

from config.llm_settings import LLMSettings
from config.settings import VectorConfig
from utils.llm_client_manager import LLMBridge, LLMClient


class InheritanceTests(unittest.TestCase):
    def test_client_is_created_only_on_first_model_call(self):
        import asyncio
        from types import SimpleNamespace
        with patch("utils.llm_client_manager.openai.AsyncOpenAI") as sdk, \
             patch("utils.llm_client_manager.record_token_usage", new_callable=AsyncMock):
            client = LLMClient(api_key="test-key", base_url="https://test.example/v1")
            sdk.assert_not_called()
            sdk.return_value.chat.completions.create = AsyncMock(return_value=SimpleNamespace(usage=None))
            async def calls():
                await client.chat(model="test", messages=[])
                await client.chat(model="test", messages=[])
            asyncio.run(calls())
            sdk.assert_called_once()
            self.assertEqual(sdk.return_value.chat.completions.create.await_count, 2)

    def test_minimal_configuration_resolves_every_model(self):
        for level in ("FLASH", "CONVERSATION", "PERSONA", "VISION", "PRO"):
            self.assertEqual(getattr(LLMSettings, level), "custom-common", level)
        self.assertEqual(VectorConfig.MODEL_NAME, "custom-embed")
        self.assertEqual(VectorConfig.API_KEY, "embed-test-key")
        self.assertEqual(VectorConfig.BASE_URL, "https://embed.example/v1")

    def test_explicit_models_win_and_clearing_them_restores_common(self):
        overrides = {f"LLM_{level}": f"own-{level.lower()}" for level in ("CONVERSATION", "PERSONA", "VISION", "PRO")}
        with patch.dict(os.environ, overrides):
            settings = runpy.run_path(str(ROOT / "config/llm_settings.py"))["LLMSettings"]
            for level in ("CONVERSATION", "PERSONA", "VISION", "PRO"):
                self.assertEqual(getattr(settings, level), f"own-{level.lower()}")
        with patch.dict(os.environ, {key: "" for key in overrides}):
            settings = runpy.run_path(str(ROOT / "config/llm_settings.py"))["LLMSettings"]
            for level in ("CONVERSATION", "PERSONA", "VISION", "PRO"):
                self.assertEqual(getattr(settings, level), "custom-common")

    def test_no_override_shares_common_client(self):
        with patch("utils.llm_client_manager.LLMClient"):
            bridge = LLMBridge()
            for name in ("conversation", "persona", "vision"):
                self.assertIs(getattr(bridge, f"_resolve_{name}_client")(), bridge._client)

    def test_partial_override_inherits_common_credentials_and_endpoint(self):
        for name in ("conversation", "persona", "vision"):
            prefix = name.upper()
            for own_key, own_url in (("own-test-key", ""), ("", "https://own.example/v1")):
                with self.subTest(name=name, key_only=bool(own_key)), \
                     patch.object(LLMSettings, f"{prefix}_API_KEY", own_key), \
                     patch.object(LLMSettings, f"{prefix}_BASE_URL", own_url), \
                     patch("utils.llm_client_manager.LLMClient") as client:
                    bridge = LLMBridge()
                    client.reset_mock()
                    getattr(bridge, f"_resolve_{name}_client")()
                    self.assertEqual(client.call_args.kwargs["api_key"], own_key or "common-test-key")
                    self.assertEqual(client.call_args.kwargs["base_url"], own_url or "https://common.example/v1")


if __name__ == "__main__":
    try:
        result = unittest.main(exit=False).result
    finally:
        import logging
        logging.shutdown()
        ENV.stop()
        DATA.cleanup()
    sys.exit(0 if result.wasSuccessful() else 1)
