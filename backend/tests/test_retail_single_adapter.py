import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from adapters.retail_single_adapter import DEFAULT_RECAMERA_RTSP, RetailSingleAdapter


class FakeProcess:
    pid = 4321
    returncode = None


def snapshot(*, present: tuple[bool, ...], sequence: int = 1) -> dict:
    products = [
        {
            "id": index,
            "name": f"Product {index}",
            "present": value,
            "missed_frames": 0 if value else 4,
            "match_distance": 0.04,
            "confidence": 0.9 if value else 0,
        }
        for index, value in enumerate(present, start=1)
    ]
    return {
        "sequence": sequence,
        "initialized": True,
        "registered_count": len(products),
        "present_count": sum(present),
        "missing_count": len(products) - sum(present),
        "total_boxes": sum(present),
        "unregistered_count": 0,
        "missing_ids": [item["id"] for item in products if not item["present"]],
        "products": products,
    }


class RetailSingleAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.out_dir = Path(self.temp.name)
        self.adapter = RetailSingleAdapter(out_dir=self.out_dir)

    async def asyncTearDown(self):
        self.adapter._prewarm_process = None
        await self.adapter.stop_prewarm()
        self.temp.cleanup()

    def mark_stream_ready(self):
        self.adapter._prewarm_process = FakeProcess()
        (self.out_dir / "latest.jpg").write_bytes(b"\xff\xd8\xff" + b"0" * 2048)
        (self.out_dir / "prewarm_status.json").write_text(json.dumps({
            "running": True,
            "connected": True,
            "frames": 18,
            "fps": 14.8,
            "network_reachable": True,
            "rtsp_port_ready": True,
            "phase": "STREAMING",
            "source_url": DEFAULT_RECAMERA_RTSP,
            "updated_at": time.time(),
        }), encoding="utf-8")

    async def test_start_only_ensures_detected_stream_proxy(self):
        self.adapter.ensure_prewarm = AsyncMock(return_value={"running": True})
        await self.adapter.start()
        self.adapter.ensure_prewarm.assert_awaited_once_with(url=DEFAULT_RECAMERA_RTSP)
        self.assertIsNone(self.adapter.process)

    async def test_status_uses_stream_and_real_snapshot_without_rknn_readiness(self):
        self.mark_stream_ready()
        self.assertTrue(self.adapter.apply_recamera_snapshot(snapshot(present=(True, True))))
        status = self.adapter.status()
        self.assertTrue(status["ready"])
        self.assertEqual(status["source_url"], DEFAULT_RECAMERA_RTSP)
        self.assertNotIn("rknn_ready", status)
        self.assertEqual(status["present_count"], 2)
        self.assertEqual(status["inventory"][0]["source"], "recamera_websocket")

    async def test_missing_product_alert_comes_from_consecutive_real_snapshots(self):
        self.adapter.apply_recamera_snapshot(snapshot(present=(True, True), sequence=1))
        self.adapter.apply_recamera_snapshot(snapshot(present=(True, False), sequence=4))
        status = self.adapter.status()
        self.assertEqual(status["inventory"][1]["count"], 0)
        self.assertEqual(status["inventory"][1]["previous_count"], 1)
        self.assertEqual(status["alert"]["product"], "Product 2")
        self.assertEqual(status["alert"]["status"], "OUT_OF_STOCK")
        self.assertEqual(status["alert"]["action"], "RESTOCK REQUIRED")
        self.assertEqual(status["alert"]["source"], "recamera_websocket")

    async def test_invalid_or_uninitialized_message_never_fabricates_inventory(self):
        self.assertFalse(self.adapter.apply_recamera_snapshot({"products": []}))
        pending = snapshot(present=())
        pending["initialized"] = False
        self.assertTrue(self.adapter.apply_recamera_snapshot(pending))
        self.assertEqual(self.adapter.status()["inventory"], [])
        self.assertEqual(self.adapter.status()["alerts"], [])

    async def test_websocket_queue_receives_normalized_events(self):
        queue = await self.adapter.subscribe_events()
        await self.adapter._emit("status", "retail.single.status", self.adapter.status())
        event = await asyncio.wait_for(queue.get(), timeout=1)
        self.assertEqual(event["type"], "retail.single.status")
        await self.adapter.unsubscribe_events(queue)

    async def test_non_detected_source_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "/detected"):
            await self.adapter.start(url="rtsp://192.168.42.1:8554/original")


if __name__ == "__main__":
    unittest.main()
