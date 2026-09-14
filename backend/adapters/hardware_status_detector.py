from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass
class HardwareStatusSnapshot:
    items: list[dict[str, Any]]
    updated_at: float


class HardwareStatusDetector:
    def __init__(self, sales_voice, recamera, retail_single=None, poll_interval: float = 10.0):
        self.sales_voice = sales_voice
        self.recamera = recamera
        self.retail_single = retail_single
        self.poll_interval = poll_interval
        self._task: asyncio.Task[None] | None = None
        self._display_active = False
        self._snapshot = HardwareStatusSnapshot(items=[], updated_at=0.0)
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def set_display_active(self, active: bool) -> None:
        self._display_active = active

    async def start(self) -> dict[str, Any]:
        self._ensure_task()
        return self.status()

    async def stop(self) -> dict[str, Any]:
        task = self._task
        self._task = None
        if task and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        return self.status()

    def status(self) -> dict[str, Any]:
        if not self._snapshot.items:
            return {
                "running": self._task is not None and not self._task.done(),
                "display_active": self._display_active,
                "display_status": "ACTIVE" if self._display_active else "INACTIVE",
                "items": [],
                "updated_at": self._snapshot.updated_at,
            }
        return {
            "running": self._task is not None and not self._task.done(),
            "display_active": self._display_active,
            "display_status": "ACTIVE" if self._display_active else "INACTIVE",
            "items": self._snapshot.items,
            "updated_at": self._snapshot.updated_at,
        }

    async def forward_websocket(self, send_json: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        queue = await self.subscribe_events()
        try:
            await send_json({"type": "connection", "status": "connected", "demo": "hardware-status"})
            await send_json({"type": "hardware.status", "data": self.status()})
            while True:
                event = await queue.get()
                await send_json(event)
        finally:
            await self.unsubscribe_events(queue)

    async def subscribe_events(self, max_queue: int = 64) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max_queue)
        self._subscribers.add(queue)
        return queue

    async def unsubscribe_events(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def _ensure_task(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._poll_loop())

    def _cancel_task(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _poll_loop(self) -> None:
        while True:
            try:
                voice = self.sales_voice.hardware_status()
                prewarm = self.retail_single.prewarm_status() if self.retail_single else {}
                recamera = await asyncio.wait_for(
                    self.recamera.hardware_status(
                        stream_ready=bool(prewarm.get("connected", False)),
                    ),
                    timeout=3.0,
                )
                items = [
                    voice,
                    recamera,
                    {
                        "name": "RK3588 AI Engine",
                        "connected": True,
                        "state": "Ready",
                        "detected_by": "Local RKNN runtime",
                        "details": {"runtime": "RKNN", "device": "RK3588"},
                    },
                ]
                self._snapshot = HardwareStatusSnapshot(items=items, updated_at=asyncio.get_running_loop().time())
                event = {"type": "hardware.status", "data": self.status()}
                for queue in list(self._subscribers):
                    try:
                        queue.put_nowait(event)
                    except asyncio.QueueFull:
                        pass
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(self.poll_interval)
