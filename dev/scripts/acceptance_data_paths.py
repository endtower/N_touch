"""程序与数据分离的路径回归；不读取或修改实际用户数据。"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from workspace_layout import test_root, CACHE, BUILDS
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paths
from core import supervisor
from config import env_tools

TEMP_ROOT = test_root()
TEMP_ROOT.mkdir(exist_ok=True)


class DataPathsTests(unittest.TestCase):
    def test_package_descriptor_is_relative_to_program_not_cwd(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            root = Path(tmp)
            app = root / "dist" / "preview"
            app.mkdir(parents=True)
            (app / "abyss-data-dir.txt").write_text("../..\n", encoding="utf-8")
            with patch.dict(os.environ, {"ABYSS_DATA_DIR": ""}), patch("paths.app_root", return_value=app):
                self.assertEqual(paths.data_root(), root.resolve())

    def test_environment_overrides_descriptor_and_new_default_is_separated(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {"ABYSS_DATA_DIR": ""}), patch("paths.app_root", return_value=root):
                self.assertEqual(paths.data_root(), root / "data")
                (root / "abyss-data-dir.txt").write_text("missing", encoding="utf-8")
                with self.assertRaises(FileNotFoundError):
                    paths.data_root()
                with patch.dict(os.environ, {"ABYSS_DATA_DIR": str(root)}):
                    self.assertEqual(paths.data_root(), root.resolve())
            # Selecting a separate profile must not borrow another profile's secrets.
            with patch.object(env_tools, "data_root", return_value=root / "data"):
                self.assertEqual(env_tools._env_candidates(), [root / "data/.env.dev"])

    def test_legacy_data_stays_in_place_and_managed_core_shares_installation(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            root = Path(tmp)
            app = root / "app"
            app.mkdir()
            with patch.dict(os.environ, {"ABYSS_DATA_DIR": ""}), patch("paths.app_root", return_value=app):
                self.assertEqual(paths.data_root(), root / "data")
                (app / ".env.dev").write_text("# synthetic legacy profile")
                self.assertEqual(paths.data_root(), app)
                self.assertEqual(paths.cache_root(), root / "cache")
                self.assertEqual(paths.backup_root(), root / "backups")
                self.assertEqual(paths.external_root(), root / "external")
            source = app / "components/core/source"
            source.mkdir(parents=True)
            (source.parent / "release.json").write_text("{}")
            with patch.dict(os.environ, {"ABYSS_DATA_DIR": ""}), patch("paths.app_root", return_value=source):
                self.assertEqual(paths.data_root(), root / "data")
                self.assertEqual(paths.cache_root(), root / "cache")

    def test_snowluma_preserves_existing_location_and_uses_external_for_new_install(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            root = Path(tmp)
            app, data = root / "app", root / "data"
            app.mkdir(); data.mkdir()
            with patch.dict(os.environ, {"ABYSS_SNOWLUMA_DIR": ""}), \
                    patch.object(supervisor, "app_root", return_value=app), \
                    patch.object(supervisor, "data_root", return_value=data), \
                    patch.object(supervisor, "external_root", return_value=root / "external"):
                self.assertEqual(supervisor.snowluma_root(), root / "external/snowluma")
                (app / "SnowLuma").mkdir()
                self.assertEqual(supervisor.snowluma_root(), app / "SnowLuma")
                (data / "SnowLuma").mkdir()
                self.assertEqual(supervisor.snowluma_root(), data / "SnowLuma")
                with patch.dict(os.environ, {"ABYSS_SNOWLUMA_DIR": str(root / "user-chosen")}):
                    self.assertEqual(supervisor.snowluma_root(), root / "user-chosen")

    def test_empty_descriptor_does_not_silently_create_an_empty_profile(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            root = Path(tmp)
            (root / "abyss-data-dir.txt").write_text("\n", encoding="utf-8")
            with patch.dict(os.environ, {"ABYSS_DATA_DIR": ""}), patch("paths.app_root", return_value=root):
                with self.assertRaises(ValueError):
                    paths.data_root()


if __name__ == "__main__":
    unittest.main()
