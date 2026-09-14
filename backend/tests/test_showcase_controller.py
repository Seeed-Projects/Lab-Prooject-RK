import asyncio
import tempfile
import unittest
from pathlib import Path

from adapters.sales_voice_adapter import SalesVoiceAdapter
from controllers.showcase_controller import ShowcaseSwitchController


class FakeLiveShelf:
    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self.running = False
        self.started = 0
        self.stopped = 0

    async def start(self):
        await asyncio.sleep(0.01)
        self.running = True
        self.started += 1
        self.out_dir.mkdir(parents=True, exist_ok=True)
        for camera_id in range(1, 9):
            (self.out_dir / f"latest_{camera_id}.jpg").write_bytes(b"x" * 2048)
        return self.status()

    async def stop(self):
        self.running = False
        self.stopped += 1
        return self.status()

    def status(self):
        return {"running": self.running}


class FakeSalesVoice:
    def __init__(self):
        self.running = False
        self.started = 0
        self.stopped = 0
        self.last_error = None
        self.upstream_ws = "ws://127.0.0.1:8765"

    async def start(self, language="zh"):
        await asyncio.sleep(0.01)
        self.running = True
        self.started += 1
        return self.status()

    async def stop(self):
        self.running = False
        self.stopped += 1
        return self.status()

    def status(self):
        return {"running": self.running}


class FakeReCamera:
    def __init__(self):
        self.checked = 0

    async def status(self):
        self.checked += 1
        return {"rtsp_port_open": True, "ws_port_open": True}


class FakeRetailSingle:
    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self.running = False
        self.started = 0
        self.stopped = 0
        self.last_error = None

    async def start(self):
        await asyncio.sleep(0.01)
        self.running = True
        self.started += 1
        self.out_dir.mkdir(parents=True, exist_ok=True)
        (self.out_dir / "latest.jpg").write_bytes(b"x" * 2048)
        (self.out_dir / "status.json").write_text(
            '{"running": true, "frame": 12, "fps": 6.5, "inventory": [{"id":"band_a","name":"Top Shelf","count":5,"status":"IN STOCK"}]}',
            encoding="utf-8",
        )
        return self.status()

    async def stop(self):
        self.running = False
        self.stopped += 1
        return self.status()

    def status(self):
        return {
            "running": self.running,
            "ready": self.running,
            "has_detection_result": self.running,
            "frame": 12 if self.running else 0,
            "inventory": [{"id": "band_a", "name": "Top Shelf", "count": 5, "status": "IN STOCK"}] if self.running else [],
        }


class FakeProcessManager:
    def __init__(self):
        self.runs = {}


class ShowcaseSwitchControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.live_shelf = FakeLiveShelf(Path(self.temp.name) / "shelf")
        self.sales_voice = FakeSalesVoice()
        self.recamera = FakeReCamera()
        self.retail_single = FakeRetailSingle(Path(self.temp.name) / "single")
        self.controller = ShowcaseSwitchController(
            live_shelf=self.live_shelf,
            sales_voice=self.sales_voice,
            recamera=self.recamera,
            retail_single=self.retail_single,
            process_manager=FakeProcessManager(),
        )
        self.controller._voice_ready = self._voice_ready

    async def asyncTearDown(self):
        await self.controller.stop()
        try:
            await self.controller.wait_for_idle(timeout=1)
        except Exception:
            pass
        self.temp.cleanup()

    async def _voice_ready(self):
        return self.sales_voice.running

    async def test_multi_to_voice_switch_reaches_running(self):
        first = await self.controller.switch("multi")
        self.assertEqual(first["state"], "SWITCHING")
        await self.controller.wait_for_idle(timeout=2)
        self.assertEqual(self.controller.get_status()["active_demo"], "multi")

        second = await self.controller.switch("voice", {"language": "zh"})
        self.assertEqual(second["state"], "SWITCHING")
        self.assertEqual(second["target_demo"], "voice")
        await self.controller.wait_for_idle(timeout=2)

        status = self.controller.get_status()
        self.assertEqual(status["state"], "RUNNING")
        self.assertEqual(status["active_demo"], "voice")
        self.assertFalse(self.live_shelf.running)
        self.assertTrue(self.sales_voice.running)

    async def test_voice_to_single_switch_reaches_running(self):
        await self.controller.switch("voice")
        await self.controller.wait_for_idle(timeout=2)

        result = await self.controller.switch("single")
        self.assertEqual(result["target_demo"], "single")
        await self.controller.wait_for_idle(timeout=2)

        status = self.controller.get_status()
        self.assertEqual(status["state"], "RUNNING")
        self.assertEqual(status["active_demo"], "single")
        self.assertFalse(self.sales_voice.running)
        self.assertTrue(self.retail_single.running)

    async def test_fast_consecutive_switch_only_last_generation_wins(self):
        await self.controller.switch("multi")
        await self.controller.switch("voice")
        final = await self.controller.switch("single")
        self.assertEqual(final["generation"], 3)

        await self.controller.wait_for_idle(timeout=2)
        status = self.controller.get_status()

        self.assertEqual(status["generation"], 3)
        self.assertEqual(status["state"], "RUNNING")
        self.assertEqual(status["active_demo"], "single")
        self.assertFalse(self.live_shelf.running)
        self.assertFalse(self.sales_voice.running)
        self.assertTrue(self.retail_single.running)

    async def test_single_showcase_can_wait_for_camera_without_switch_error(self):
        self.retail_single.status = lambda: {
            "running": True,
            "ready": False,
            "has_detection_result": False,
            "showcase_ready": True,
        }

        self.assertTrue(await self.controller._single_ready())


class VoiceShowcaseReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_voice_showcase_does_not_wait_for_first_turn(self):
        adapter = SalesVoiceAdapter()
        adapter.status = lambda: {
            "showcase_ready": True,
            "service_ready": False,
            "ai_ready": False,
            "last_turn": None,
        }

        self.assertTrue(await adapter.is_ready())


if __name__ == "__main__":
    unittest.main()
