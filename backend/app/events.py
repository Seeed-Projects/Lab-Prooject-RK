from __future__ import annotations

import asyncio
import json
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from .domain import utc_now


class EventBus:
    def __init__(self, replay_size: int = 1000):
        self.replay_size = replay_size
        self._seq: dict[str, int] = defaultdict(int)
        self._events: dict[str, deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=self.replay_size)
        )
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = asyncio.Lock()

    async def publish(
        self,
        demo_id: str,
        run_id: str,
        channel: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        persist_path: Path | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            self._seq[run_id] += 1
            event = {
                "schema_version": "1.0",
                "event_id": f"evt_{uuid.uuid4().hex}",
                "demo_id": demo_id,
                "run_id": run_id,
                "seq": self._seq[run_id],
                "channel": channel,
                "type": event_type,
                "timestamp": utc_now(),
                "payload": payload or {},
            }
            self._events[run_id].append(event)
            subscribers = tuple(self._subscribers)
        if persist_path:
            persist_path.parent.mkdir(parents=True, exist_ok=True)
            with persist_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        for queue in subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A slow client must never block a demo's stdout pipe.
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
        return event

    async def subscribe(self, max_queue: int = 256) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max_queue)
        async with self._lock:
            self._subscribers.add(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            self._subscribers.discard(queue)

    def replay(self, run_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        return [event for event in self._events.get(run_id, ()) if event["seq"] > after_seq]

