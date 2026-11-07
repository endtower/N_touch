"""Isolated relocation regression; does not start services or install versions."""
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
import sys

from workspace_layout import SOURCE, test_root
from repair_update_history import repair

sys.path.insert(0, str(SOURCE))
import manager_updates
from manager_updates import Updates, write_json


class UpdateHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="update-history-", dir=test_root())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.root / "app"
        self.data = self.root / "data"

    def test_migration_keeps_version_catalogs_in_live_management_state(self):
        import deploy_source_layout
        catalog = self.root / "manager/updates/catalog/snowluma.json"
        archive = self.root / "manager/updates/packages/snowluma/2.0.3/release.json"
        write_json(catalog, {"source": "same", "releases": []})
        write_json(archive, {"version": "2.0.3"})
        with patch.object(deploy_source_layout, "ROOT", self.root):
            destinations = dict(deploy_source_layout.plan())
        self.assertEqual(destinations[catalog], self.data / "manager/updates/catalog/snowluma.json")
        self.assertEqual(destinations[archive], self.root / "backups/legacy-updates/packages/snowluma/2.0.3/release.json")

    def test_restored_catalog_and_backup_appear_in_existing_manager(self):
        source = "https://example.test/snowluma"
        write_json(self.app / "update_defaults.json", {"sources": {"snowluma": source}})
        archived = self.root / "backups/legacy-updates/catalog/snowluma.json"
        write_json(archived, {"source": source, "releases": [{"version": "2.0.0"}, {"version": "2.0.3"}]})
        folder = self.root / "backups/legacy-updates/jobs/old-install"
        write_json(folder / "job.json", {"component": "core"})
        write_json(folder / "journal.json", {"state": "installed"})
        receipt = {"version": "0.3.0", "target_root": str(self.root / "dist/Abyss"),
                   "files": {"core.exe": "original-file-hash"}}
        write_json(folder / "previous-receipt.json", receipt)
        with patch.object(manager_updates, "app_root", return_value=self.app), \
                patch.object(manager_updates, "data_root", return_value=self.data), \
                patch.object(manager_updates, "cache_root", return_value=self.root / "cache"), \
                patch.object(manager_updates, "backup_root", return_value=self.root / "backups"):
            manager = Updates(types.SimpleNamespace(), "unused", "http://127.0.0.1:1")
            self.assertEqual(manager.versions("snowluma"), {})
            self.assertEqual(manager.versions("core"), {})
            result = repair(self.root)
            self.assertEqual(set(manager.versions("snowluma")), {"2.0.0", "2.0.3"})
            self.assertIn("0.3.0", manager.versions("core"))
        changed = json.loads((folder / "previous-receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(changed, {**receipt, "target_root": str(self.app)})
        saved = Path(result["backup"]) / "before" / (folder / "previous-receipt.json").relative_to(self.root)
        self.assertEqual(json.loads(saved.read_text(encoding="utf-8")), receipt)
        self.assertEqual(repair(self.root)["catalogs_restored"], 0)

    def test_current_feed_and_newer_metadata_win(self):
        old = self.root / "backups/legacy-updates/catalog/snowluma.json"
        active = self.data / "manager/updates/catalog/snowluma.json"
        write_json(old, {"source": "same", "releases": [{"version": "2.0.3", "notes": "old"}, {"version": "2.0.0"}]})
        write_json(active, {"source": "same", "releases": [{"version": "2.0.3", "notes": "new"}]})
        repair(self.root)
        releases = json.loads(active.read_text(encoding="utf-8"))["releases"]
        self.assertEqual(next(v for v in releases if v["version"] == "2.0.3")["notes"], "new")
        write_json(active, {"source": "intentionally-changed", "releases": []})
        before = active.read_bytes()
        repair(self.root)
        self.assertEqual(active.read_bytes(), before)

    def test_unrelated_installations_and_active_updates_are_untouched(self):
        folder = self.root / "backups/legacy-updates/jobs/unrelated"
        write_json(folder / "job.json", {"component": "core"})
        write_json(folder / "journal.json", {"state": "installed"})
        write_json(folder / "previous-receipt.json", {"target_root": str(self.root / "another-install"), "files": {}})
        before = (folder / "previous-receipt.json").read_bytes()
        repair(self.root)
        self.assertEqual((folder / "previous-receipt.json").read_bytes(), before)
        write_json(self.data / "manager/updates/active.json", {"id": "busy"})
        with self.assertRaises(ValueError):
            repair(self.root)


if __name__ == "__main__":
    unittest.main()
