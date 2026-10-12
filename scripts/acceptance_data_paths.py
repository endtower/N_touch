"""程序与数据分离的路径回归；不读取或修改实际用户数据。"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paths


class DataPathsTests(unittest.TestCase):
    def test_package_descriptor_is_relative_to_program_not_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / "dist" / "preview"
            app.mkdir(parents=True)
            (app / "abyss-data-dir.txt").write_text("../..\n", encoding="utf-8")
            with patch.dict(os.environ, {"ABYSS_DATA_DIR": ""}), patch("paths.app_root", return_value=app):
                self.assertEqual(paths.data_root(), root.resolve())

    def test_environment_overrides_descriptor_and_portable_default_is_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {"ABYSS_DATA_DIR": ""}), patch("paths.app_root", return_value=root):
                self.assertEqual(paths.data_root(), root)
                (root / "abyss-data-dir.txt").write_text("missing", encoding="utf-8")
                with self.assertRaises(FileNotFoundError):
                    paths.data_root()
                with patch.dict(os.environ, {"ABYSS_DATA_DIR": str(root)}):
                    self.assertEqual(paths.data_root(), root.resolve())

    def test_empty_descriptor_does_not_silently_create_an_empty_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "abyss-data-dir.txt").write_text("\n", encoding="utf-8")
            with patch.dict(os.environ, {"ABYSS_DATA_DIR": ""}), patch("paths.app_root", return_value=root):
                with self.assertRaises(ValueError):
                    paths.data_root()


if __name__ == "__main__":
    unittest.main()
