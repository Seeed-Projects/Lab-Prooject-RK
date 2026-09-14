import json
import tempfile
import unittest
from pathlib import Path

from app.domain import NotFoundError
from app.registry import DemoRegistry


class RegistryTests(unittest.TestCase):
    def test_scans_valid_manifest_and_reports_invalid_one(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            valid_dir = root / "plugins" / "valid-demo"
            valid_dir.mkdir(parents=True)
            manifest = {
                "name": "valid-demo",
                "type": "batch/test",
                "entry": ["python3", "demo.py"],
                "port": None,
                "description": "test",
                "project_root": str(project),
            }
            (valid_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            invalid_dir = root / "plugins" / "invalid"
            invalid_dir.mkdir()
            (invalid_dir / "manifest.json").write_text("{}", encoding="utf-8")

            registry = DemoRegistry(root / "plugins")
            registry.scan()

            self.assertEqual([item.name for item in registry.list()], ["valid-demo"])
            self.assertTrue(registry.errors)
            self.assertEqual(registry.get("valid-demo").project_root, project.resolve())
            with self.assertRaises(NotFoundError):
                registry.get("missing")

    def test_resolves_relative_project_root_from_manifest_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            plugin = root / "plugins" / "relative-demo"
            plugin.mkdir(parents=True)
            (plugin / "manifest.json").write_text(json.dumps({
                "name": "relative-demo",
                "type": "batch/test",
                "entry": ["python3", "demo.py"],
                "port": None,
                "description": "test",
                "project_root": "../../project",
            }), encoding="utf-8")

            registry = DemoRegistry(root / "plugins")
            registry.scan()

            self.assertFalse(registry.errors)
            self.assertEqual(registry.get("relative-demo").project_root, project.resolve())


if __name__ == "__main__":
    unittest.main()
