import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from adapters.hardware_status_detector import HardwareStatusDetector
from adapters.recamera_adapter import ReCameraAdapter


class FakeVoice:
    def hardware_status(self):
        return {"name": "ReSpeaker", "connected": True, "state": "Connected", "details": {}}


class FakeSingle:
    def prewarm_status(self):
        return {"running": True, "connected": True}


class FakeReCamera:
    async def hardware_status(self, *, stream_ready=False):
        return {
            "name": "reCamera",
            "connected": stream_ready,
            "state": "Connected" if stream_ready else "Disconnected",
            "details": {"stream_ready": stream_ready},
        }


class HardwareStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_detector_merges_actual_detected_stream_frame_state(self):
        detector = HardwareStatusDetector(FakeVoice(), FakeReCamera(), FakeSingle(), poll_interval=0.01)
        await detector.start()
        await asyncio.sleep(0.03)
        recamera = next(item for item in detector.status()["items"] if item["name"] == "reCamera")
        self.assertTrue(recamera["details"]["stream_ready"])
        await detector.stop()

    async def test_recamera_states_are_network_rtsp_and_actual_video(self):
        adapter = ReCameraAdapter()
        with patch.object(adapter, "_network_reachable_async", AsyncMock(return_value=False)), patch.object(
            adapter, "_port_open_async", AsyncMock(side_effect=lambda port: port == 8554)
        ):
            status = await adapter.hardware_status(stream_ready=False)
        self.assertTrue(status["details"]["network_connected"])
        self.assertTrue(status["details"]["rtsp_port_open"])
        self.assertFalse(status["details"]["stream_ready"])
        self.assertEqual(status["state"], "Connecting")
        self.assertNotIn("usb_connected", status["details"])


if __name__ == "__main__":
    unittest.main()
