"""Translate real ShelfPipeline inventory events into showcase business events."""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any


class ShelfEventTracker:
    def __init__(self, camera_id: int, max_timeline: int = 64) -> None:
        self.camera_id = camera_id
        self._active_alerts: dict[str, dict[str, Any]] = {}
        self._timeline: deque[dict[str, Any]] = deque(maxlen=max_timeline)
        self._lock = threading.Lock()

    def ingest(self, events, inventory) -> None:
        if not events:
            return
        snapshots = {row.id: row for row in inventory.snapshot()}
        with self._lock:
            for event in events:
                row = snapshots.get(event.region_id)
                if row is None:
                    continue
                kind = str(getattr(event.kind, "value", event.kind))
                removed = kind == "ITEM REMOVED"
                current = int(row.count)
                previous = current + int(event.delta) if removed else max(0, current - int(event.delta))
                now = time.time()
                display_time = time.strftime("%H:%M:%S", time.localtime(now))
                status = str(getattr(row.status, "value", row.status))
                normalized_status = status.replace(" ", "_")
                product = str(row.name).upper()

                self._timeline.appendleft({
                    "time": display_time,
                    "timestamp": now,
                    "camera": self.camera_id,
                    "product": product,
                    "previous_count": previous,
                    "current_count": current,
                    "kind": kind,
                    "status": normalized_status,
                    "title": f"{product} stock {'decreased' if removed else 'increased'}: {previous} -> {current}",
                    "source": "detection",
                })

                if removed and normalized_status in {"LOW_STOCK", "OUT_OF_STOCK"}:
                    alert = {
                        "type": "stock_alert",
                        "camera": self.camera_id,
                        "product": product,
                        "region_id": row.id,
                        "previous_count": previous,
                        "current_count": current,
                        "status": normalized_status,
                        "action": "RESTOCK",
                        "display_status": status,
                        "display_action": "Restock Required",
                        "time": display_time,
                        "timestamp": now,
                        "source": "detection",
                    }
                    self._active_alerts[row.id] = alert
                    self._timeline.appendleft({
                        **alert,
                        "title": f"RESTOCK REQUIRED: {product}",
                    })
                elif not removed and normalized_status == "IN_STOCK":
                    recovered = self._active_alerts.pop(row.id, None)
                    if recovered:
                        self._timeline.appendleft({
                            "time": display_time,
                            "timestamp": now,
                            "camera": self.camera_id,
                            "product": product,
                            "previous_count": previous,
                            "current_count": current,
                            "kind": kind,
                            "status": normalized_status,
                            "title": f"{product} stock recovered",
                            "source": "detection",
                        })

    def snapshot(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        with self._lock:
            alerts = sorted(self._active_alerts.values(), key=lambda item: item["timestamp"], reverse=True)
            return [dict(item) for item in alerts], [dict(item) for item in self._timeline]
