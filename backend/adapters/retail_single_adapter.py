from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import time
from pathlib import Path
from typing import Any, Awaitable, Callable


DEFAULT_RECAMERA_RTSP = "rtsp://192.168.42.1:8554/detected"
DEFAULT_RECAMERA_WS = "ws://192.168.42.1:9002"


class RetailSingleAdapter:
    """Proxy reCamera's detected stream and product-state protocol.

    reCamera owns all Single Camera inference. This adapter deliberately has no
    model, detector, or local shelf-business dependency.
    """

    def __init__(
        self,
        *,
        project_root: Path | None = None,
        runner: Path | None = None,
        config: Path | None = None,
        out_dir: Path = Path("/tmp/rk3588_demo_hub/retail_single"),
        rtsp_url: str = DEFAULT_RECAMERA_RTSP,
        websocket_url: str = DEFAULT_RECAMERA_WS,
        reconnect_delay: float = 2.0,
        capture_python: Path = Path("python3"),
        backend_root: Path | None = None,
    ):
        # Retained keyword arguments keep older integrations source-compatible;
        # none of them are used to load or execute local inference.
        del project_root, runner, config
        self.out_dir = out_dir
        self._rtsp_url = rtsp_url
        self.websocket_url = websocket_url
        self.reconnect_delay = reconnect_delay
        self.capture_python = capture_python
        self.backend_root = backend_root or Path(__file__).resolve().parents[1]
        self.last_error: str | None = None
        self.started_at: float | None = None
        self._prewarm_process: asyncio.subprocess.Process | None = None
        self._event_task: asyncio.Task[None] | None = None
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._display_active = False
        self._websocket_connected = False
        self._latest_snapshot: dict[str, Any] | None = None
        self._latest_snapshot_received_at: float | None = None
        self._inventory: list[dict[str, Any]] = []
        self._alerts: list[dict[str, Any]] = []
        self._timeline: list[dict[str, Any]] = []
        self._previous_presence: dict[int, bool] = {}

    @property
    def process(self) -> None:
        """Compatibility signal: Single never owns a local inference process."""
        return None

    @property
    def source_url(self) -> str:
        return self._rtsp_url

    def set_display_active(self, active: bool) -> None:
        self._display_active = active
        self.out_dir.mkdir(parents=True, exist_ok=True)
        active_path = self.out_dir / "capture_active"
        if active:
            active_path.touch(exist_ok=True)
        else:
            active_path.unlink(missing_ok=True)

    async def start(self, *, url: str | None = None) -> dict[str, Any]:
        source = url or self._rtsp_url
        self._require_detected_source(source)
        await self.ensure_prewarm(url=source)
        return self.status()

    async def ensure_prewarm(self, *, url: str) -> dict[str, Any]:
        self._require_detected_source(url)
        self._rtsp_url = url
        self._ensure_event_task()
        if self._prewarm_process and self._prewarm_process.returncode is None:
            return self.prewarm_status()

        prewarm_runner = self.backend_root / "tools" / "retail_single_prewarm.py"
        if not prewarm_runner.is_file():
            raise FileNotFoundError(f"prewarm runner missing: {prewarm_runner}")
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._cleanup_capture_outputs()
        python_bin = str(self.capture_python) if self.capture_python.is_file() else (shutil.which("python3") or "python3")
        self._prewarm_process = await asyncio.create_subprocess_exec(
            python_bin,
            str(prewarm_runner),
            "--rtsp-url",
            url,
            "--out-dir",
            str(self.out_dir),
            "--idle-fps",
            "1",
            "--active-fps",
            "15",
            "--first-connect-grace",
            "360",
            "--retry-interval",
            "10",
            cwd=str(self.backend_root),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        self.started_at = time.monotonic()
        return self.prewarm_status()

    async def stop(self) -> dict[str, Any]:
        # Page routing only changes display activity. The hardware connection is
        # intentionally resident across demo switches.
        return self.status()

    async def stop_runtime(self) -> dict[str, Any]:
        # Kept for lifecycle compatibility; there is no Single inference runtime.
        self.set_display_active(False)
        return self.status()

    async def stop_prewarm(self) -> None:
        self.set_display_active(False)
        event_task = self._event_task
        self._event_task = None
        if event_task and not event_task.done():
            event_task.cancel()
            try:
                await event_task
            except asyncio.CancelledError:
                pass
        self._websocket_connected = False

        proc = self._prewarm_process
        if not proc or proc.returncode is not None:
            self._prewarm_process = None
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            self._prewarm_process = None
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
        self._prewarm_process = None

    def status(self) -> dict[str, Any]:
        prewarm = self.prewarm_status()
        running = bool(prewarm["running"])
        stream_ready = bool(prewarm["connected"])
        initialized = bool(self._latest_snapshot and self._latest_snapshot.get("initialized"))
        if stream_ready:
            runtime_status = "READY"
        elif running:
            runtime_status = "BOOTING"
        else:
            runtime_status = "SUSPENDED"
        display_status = "ACTIVE" if self._display_active else "INACTIVE"
        return {
            "running": running,
            "ready": stream_ready,
            "showcase_ready": self._display_active and running,
            "display_active": self._display_active,
            "display_status": display_status,
            "runtime_status": runtime_status,
            "background_runtime": runtime_status,
            "connection_phase": prewarm["phase"],
            "network_reachable": prewarm["network_reachable"],
            "rtsp_port_ready": prewarm["rtsp_port_ready"],
            "rtsp_connected": stream_ready,
            "stream_ready": stream_ready,
            "websocket_connected": self._websocket_connected,
            "data_initialized": initialized,
            "has_detection_result": initialized,
            "frame": prewarm["frames"],
            "fps": prewarm["fps"],
            "total_fps_display": prewarm["fps"],
            "timestamp": prewarm["updated_at"],
            "preview_url": "/api/v1/retail-single/frame.jpg",
            "stream_url": "/api/v1/retail-single/mjpeg",
            "source_url": self._rtsp_url,
            "websocket_url": self.websocket_url,
            "registered_count": self._snapshot_int("registered_count"),
            "present_count": self._snapshot_int("present_count"),
            "missing_count": self._snapshot_int("missing_count"),
            "inventory": list(self._inventory),
            "detected_products": [item for item in self._inventory if item["present"]],
            "low_stock": list(self._alerts),
            "alerts": list(self._alerts),
            "alert": self._alerts[0] if self._alerts else None,
            "timeline": list(self._timeline),
            "inventory_snapshot": self.inventory_snapshot(),
            "last_error": self.last_error,
            "status_path": str(self.out_dir / "prewarm_status.json"),
            "frame_path": str(self.frame_path()),
            "prewarm": prewarm,
        }

    def prewarm_status(self) -> dict[str, Any]:
        proc = self._prewarm_process
        running = bool(proc and proc.returncode is None)
        raw = self._read_json(self.out_dir / "prewarm_status.json")
        updated_at = float(raw.get("updated_at", 0.0) or 0.0)
        fresh = updated_at > 0 and time.time() - updated_at < 10.0
        connected = running and fresh and bool(raw.get("connected", False)) and self.frame_path().is_file()
        return {
            "running": running,
            "connected": connected,
            "frames": int(raw.get("frames", 0) or 0),
            "fps": float(raw.get("fps", 0.0) or 0.0),
            "source_url": raw.get("source_url") or self._rtsp_url,
            "network_reachable": bool(raw.get("network_reachable", False)),
            "rtsp_port_ready": bool(raw.get("rtsp_port_ready", False)),
            "phase": raw.get("phase") or ("WAITING_FOR_RECAMERA" if running else "SUSPENDED"),
            "connection_attempts": int(raw.get("connection_attempts", 0) or 0),
            "waiting_seconds": float(raw.get("waiting_seconds", 0.0) or 0.0),
            "first_connect_grace_seconds": float(raw.get("first_connect_grace_seconds", 360.0) or 360.0),
            "retry_interval_seconds": float(raw.get("retry_interval_seconds", 10.0) or 10.0),
            "updated_at": updated_at,
            "last_error": raw.get("last_error"),
        }

    def frame_path(self) -> Path:
        return self.out_dir / "latest.jpg"

    def status_path(self) -> Path:
        return self.out_dir / "prewarm_status.json"

    def latest_video_path(self) -> Path:
        return self.out_dir / "latest.mp4"

    async def subscribe_events(self, max_queue: int = 128) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max_queue)
        self._subscribers.add(queue)
        return queue

    async def unsubscribe_events(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    async def forward_websocket(self, send_json: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        queue = await self.subscribe_events()
        try:
            await send_json({"type": "connection", "status": "connected", "demo": "retail-single"})
            await send_json({"type": "retail.single.status", "data": self.status()})
            while True:
                await send_json(await queue.get())
        finally:
            await self.unsubscribe_events(queue)

    async def is_ready(self) -> bool:
        # A 3-6 minute first RTSP connection must not block entering the page.
        return bool(self.status()["showcase_ready"])

    async def events(self, send_json: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        await self.forward_websocket(send_json)

    def apply_recamera_snapshot(self, payload: dict[str, Any]) -> bool:
        """Validate and apply one real product_quantity_rec WebSocket snapshot."""
        if not self._is_protocol_snapshot(payload):
            return False

        products = payload["products"]
        initialized = payload["initialized"]
        inventory: list[dict[str, Any]] = []
        alerts: list[dict[str, Any]] = []
        changes: list[dict[str, Any]] = []
        current_presence: dict[int, bool] = {}
        now = time.time()

        for product in products:
            product_id = int(product["id"])
            present = bool(product["present"])
            current_presence[product_id] = present
            previous_present = self._previous_presence.get(product_id)
            name = str(product.get("name") or f"Product {product_id}")
            current_count = 1 if present else 0
            previous_count = None if previous_present is None else (1 if previous_present else 0)
            row = {
                "id": product_id,
                "name": name,
                "product": name,
                "count": current_count,
                "previous_count": previous_count,
                "current_count": current_count,
                "present": present,
                "status": "IN STOCK" if present else "OUT OF STOCK",
                "source": "recamera_websocket",
            }
            inventory.append(row)

            if initialized and previous_present is not None and previous_present != present:
                change = {
                    "type": "stock_change",
                    "product_id": product_id,
                    "product": name,
                    "previous_count": 1 if previous_present else 0,
                    "current_count": current_count,
                    "status": row["status"],
                    "source": "recamera_websocket",
                    "timestamp": now,
                }
                changes.append(change)
                self._timeline.insert(0, change)

            if initialized and not present:
                alerts.append({
                    "type": "stock_alert",
                    "product_id": product_id,
                    "product": name,
                    "previous_count": previous_count,
                    "current_count": 0,
                    "status": "OUT_OF_STOCK",
                    "action": "RESTOCK REQUIRED",
                    "source": "recamera_websocket",
                    "timestamp": now,
                })

        self._latest_snapshot = dict(payload)
        self._latest_snapshot_received_at = now
        self._inventory = inventory if initialized else []
        self._alerts = alerts
        if initialized:
            self._previous_presence = current_presence
        self._timeline = self._timeline[:30]
        self.last_error = None
        self._schedule_snapshot_events(changes)
        return True

    def _ensure_event_task(self) -> None:
        if self._event_task and not self._event_task.done():
            return
        self._event_task = asyncio.create_task(self._upstream_event_loop(), name="recamera-product-events")

    async def _upstream_event_loop(self) -> None:
        try:
            import websockets
        except Exception as exc:
            self.last_error = f"WebSocket client unavailable: {exc}"
            return

        while True:
            try:
                async with websockets.connect(
                    self.websocket_url,
                    open_timeout=3,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=1,
                    proxy=None,
                ) as websocket:
                    self._websocket_connected = True
                    self.last_error = None
                    await self._emit("status", "retail.single.status", self.status())
                    async for message in websocket:
                        payload = self._decode_json(message)
                        if payload is not None:
                            self.apply_recamera_snapshot(payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._websocket_connected = False
                # Device absence and its 3-6 minute startup are expected states,
                # not customer-facing runtime errors.
                self.last_error = None
                await self._emit("status", "retail.single.status", self.status())
                await asyncio.sleep(self.reconnect_delay)
            finally:
                self._websocket_connected = False

    def _schedule_snapshot_events(self, changes: list[dict[str, Any]]) -> None:
        async def emit_all() -> None:
            await self._emit("status", "retail.single.status", self.status())
            await self._emit("inventory", "retail.single.inventory", {"inventory": self._inventory})
            for change in changes:
                await self._emit("inventory", "retail.single.stock_change", change)
            for alert in self._alerts:
                await self._emit("alert", "retail.single.alert", alert)

        try:
            asyncio.get_running_loop().create_task(emit_all())
        except RuntimeError:
            pass

    async def _emit(self, channel: str, event_type: str, payload: dict[str, Any]) -> None:
        event = {"type": event_type, "channel": channel, "timestamp": time.time(), "payload": payload}
        for queue in tuple(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    def _snapshot_int(self, field: str) -> int:
        if not self._latest_snapshot:
            return 0
        return int(self._latest_snapshot.get(field, 0) or 0)

    def inventory_snapshot(self, *, max_age_seconds: float = 5.0) -> dict[str, Any]:
        """Return one authoritative, freshness-checked inventory snapshot."""
        received_at = self._latest_snapshot_received_at
        age = None if received_at is None else max(0.0, time.time() - received_at)
        payload = self._latest_snapshot or {}
        initialized = bool(payload.get("initialized"))
        if not received_at:
            reason = "NO_DATA"
        elif not initialized:
            reason = "NOT_INITIALIZED"
        elif age is not None and age > max_age_seconds:
            reason = "STALE"
        else:
            reason = None

        present = [row for row in self._inventory if row.get("present")]
        names: dict[str, int] = {}
        for row in present:
            name = str(row.get("name") or row.get("product") or "Unknown product").strip()
            names[name] = names.get(name, 0) + int(row.get("count", 1) or 0)
        total_count = payload.get("total_boxes")
        if not isinstance(total_count, int) or total_count < 0:
            total_count = sum(names.values())
        present_products = [
            {"id": row["id"], "name": row["name"], "count": row["count"]}
            for row in present
        ]
        missing_products = [
            {"id": row["id"], "name": row["name"]}
            for row in self._inventory
            if not row.get("present")
        ]
        return {
            "source": "recamera",
            "sequence": payload.get("sequence"),
            "received_at": received_at,
            "data_age_seconds": round(age, 3) if age is not None else None,
            "data_valid": reason is None,
            "invalid_reason": reason,
            "initialized": initialized,
            "total_count": int(total_count),
            "type_count": len(names),
            "present_products": present_products,
            "present_product_counts": names,
            "missing_products": missing_products,
            "registered_count": self._snapshot_int("registered_count"),
            "missing_count": self._snapshot_int("missing_count"),
        }

    def _cleanup_capture_outputs(self) -> None:
        for name in ("latest.jpg", "prewarm_status.json"):
            (self.out_dir / name).unlink(missing_ok=True)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _decode_json(message: str | bytes) -> dict[str, Any] | None:
        try:
            value = json.loads(message)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _is_protocol_snapshot(payload: dict[str, Any]) -> bool:
        required = {
            "sequence",
            "initialized",
            "registered_count",
            "present_count",
            "missing_count",
            "missing_ids",
            "products",
        }
        if not required.issubset(payload):
            return False
        if not isinstance(payload["initialized"], bool):
            return False
        if not isinstance(payload["products"], list) or not isinstance(payload["missing_ids"], list):
            return False
        for product in payload["products"]:
            if not isinstance(product, dict) or "id" not in product or not isinstance(product.get("present"), bool):
                return False
        return True

    def _require_detected_source(self, url: str) -> None:
        if url != self._rtsp_url:
            raise ValueError(f"Single Camera source is fixed to {self._rtsp_url}")
