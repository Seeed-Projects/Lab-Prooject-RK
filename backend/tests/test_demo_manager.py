import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.events import EventBus
from app.process_manager import ProcessManager
from app.registry import DemoRegistry
from app.resources import ResourceManager
from demo_manager import DemoManager, DemoState, DemoSwitchError


class DemoManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        plugins = root / "plugins"
        for name in ("demo-one", "demo-two"):
            project = root / name
            project.mkdir()
            (project / "demo.py").write_text(
                "import time\nprint('ready', flush=True)\ntime.sleep(30)\n",
                encoding="utf-8",
            )
            plugin = plugins / name
            plugin.mkdir(parents=True)
            (plugin / "manifest.json").write_text(json.dumps({
                "name": name,
                "type": "streaming/test",
                "entry": ["python3", "demo.py"],
                "port": None,
                "description": name,
                "project_root": str(project),
                "adapter": {"type": "process"},
                "lifecycle": {
                    "single_instance": True,
                    "start_timeout_sec": 2,
                    "stop_timeout_sec": 2,
                },
                "resources": {"npu": {"mode": "exclusive"}},
            }), encoding="utf-8")
        registry = DemoRegistry(plugins)
        registry.scan()
        resources = ResourceManager()
        self.process_manager = ProcessManager(registry, root / "var", EventBus(), resources)
        self.demo_manager = DemoManager(self.process_manager)

    async def asyncTearDown(self):
        await self.process_manager.shutdown()
        self.temp.cleanup()

    async def test_switch_stops_current_releases_resource_and_starts_target(self):
        first = await self.demo_manager.switch_demo("demo-one")
        self.assertEqual(first.state, DemoState.RUNNING)
        first_run_id = first.current_run_id

        second = await self.demo_manager.switch_demo("demo-two")

        self.assertEqual(second.state, DemoState.RUNNING)
        self.assertEqual(second.current_demo_id, "demo-two")
        self.assertEqual(self.process_manager.get(first_run_id).state, "stopped")
        resources = self.process_manager.resources.snapshot()
        self.assertEqual(resources["npu"]["demo_id"], "demo-two")

    async def test_unknown_target_does_not_stop_current(self):
        first = await self.demo_manager.switch_demo("demo-one")
        with self.assertRaises(Exception):
            await self.demo_manager.switch_demo("missing-demo")
        self.assertEqual(self.process_manager.get(first.current_run_id).state, "running")

    async def test_start_failure_enters_error(self):
        with patch.object(
            self.process_manager,
            "start",
            new=AsyncMock(side_effect=OSError("spawn denied")),
        ):
            with self.assertRaises(DemoSwitchError):
                await self.demo_manager.switch_demo("demo-two")
        self.assertEqual(self.demo_manager.status.state, DemoState.ERROR)
        self.assertIsNotNone(self.demo_manager.status.error)


if __name__ == "__main__":
    unittest.main()
