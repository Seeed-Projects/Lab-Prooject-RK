import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from app.events import EventBus
from app.process_manager import ProcessManager
from app.registry import DemoRegistry
from app.resources import ResourceManager


class ProcessManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        project = self.root / "project"
        project.mkdir()
        script = project / "demo.py"
        script.write_text(
            "import json, time\n"
            "print(json.dumps({'type':'turn','speaker':1,'text':'ok'}), flush=True)\n"
            "print('finished', flush=True)\n",
            encoding="utf-8",
        )
        plugin = self.root / "plugins" / "fake-demo"
        plugin.mkdir(parents=True)
        (plugin / "manifest.json").write_text(json.dumps({
            "name": "fake-demo",
            "type": "batch/test",
            "entry": ["python3", "demo.py"],
            "port": None,
            "description": "fake",
            "project_root": str(project),
            "adapter": {"type": "process"},
            "lifecycle": {"single_instance": True},
            "resources": {"npu": {"mode": "exclusive"}},
            "parameters": {},
        }), encoding="utf-8")
        self.registry = DemoRegistry(self.root / "plugins")
        self.registry.scan()
        self.bus = EventBus()
        self.manager = ProcessManager(self.registry, self.root / "var", self.bus, ResourceManager())

    async def asyncTearDown(self):
        await self.manager.shutdown()
        self.temp.cleanup()

    async def test_process_completion_logs_and_events(self):
        run = await self.manager.start("fake-demo")
        for _ in range(100):
            if self.manager.get(run.run_id).state == "completed":
                break
            await asyncio.sleep(0.01)
        current = self.manager.get(run.run_id)
        self.assertEqual(current.state, "completed")
        self.assertEqual(current.exit_code, 0)
        logs = self.manager.logs(run.run_id)
        self.assertIn("finished", logs["logs"]["stdout"])
        event_types = [event["type"] for event in self.bus.replay(run.run_id)]
        self.assertIn("log.stdout", event_types)
        self.assertIn("hub.run.completed", event_types)

    async def test_running_process_can_be_stopped_as_a_process_group(self):
        project = self.registry.get("fake-demo").project_root
        (project / "demo.py").write_text(
            "import time\nprint('ready', flush=True)\ntime.sleep(30)\n",
            encoding="utf-8",
        )
        run = await self.manager.start("fake-demo")
        self.assertEqual(run.state, "running")
        await self.manager.stop(run.run_id)
        for _ in range(100):
            if self.manager.get(run.run_id).state == "stopped":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.manager.get(run.run_id).state, "stopped")


if __name__ == "__main__":
    unittest.main()
