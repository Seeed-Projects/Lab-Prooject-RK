from __future__ import annotations

import asyncio
import os
import signal
import shutil
import time
from pathlib import Path
from typing import Any, Awaitable, Callable


class MultiCameraAdapter:
    def __init__(
        self,
        *,
        project_root: Path | None = None,
        runner: Path | None = None,
        config: Path | None = None,
        inference_root: Path | None = None,
        out_dir: Path = Path("/tmp/rk3588_demo_hub/shelf_live"),
        streams: int = 4,
    ):
        backend_root = Path(__file__).resolve().parents[1]
        package_root = backend_root.parent
        self.project_root = project_root or backend_root
        self.runner = runner or backend_root / "tools" / "live_shelf_8stream.py"
        self.inference_root = inference_root or package_root / "demos" / "multi-camera-shelf"
        self.config = config or self.inference_root / "configs" / "runtime.json"
        self.out_dir = out_dir
        self.streams = streams
        self.process: asyncio.subprocess.Process | None = None
        self.last_error: str | None = None
        self.started_at: float | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._wait_task: asyncio.Task[None] | None = None
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._last_signature: tuple[Any, ...] | None = None
        self._announced_alerts: set[tuple[Any, ...]] = set()
        self._display_active = False
        self._prepared = False
        self._latest_status: dict[str, Any] | None = None
        self._stop_task: asyncio.Task[dict[str, Any]] | None = None
        self._start_lock = asyncio.Lock()

    async def prepare(self) -> dict[str, Any]:
        self._prepared = True
        self.out_dir.mkdir(parents=True, exist_ok=True)
        return self.status()

    async def start(self) -> dict[str, Any]:
        pending_stop = self._stop_task
        if pending_stop and pending_stop is not asyncio.current_task() and not pending_stop.done():
            await asyncio.shield(pending_stop)
        if pending_stop is self._stop_task:
            self._stop_task = None

        async with self._start_lock:
            if self.process and self.process.returncode is None:
                return self.status()
            self._prepared = True
            if not self.runner.is_file():
                raise FileNotFoundError(f"runner missing: {self.runner}")
            if not self.config.is_file():
                raise FileNotFoundError(f"config missing: {self.config}")

            self._cleanup_outputs()
            env = dict(os.environ)
            env.setdefault("PYTHONUNBUFFERED", "1")
            env["PYTHONPATH"] = str(self.inference_root)
            python_bin = str(self.inference_root / ".venv" / "bin" / "python")
            self.process = await asyncio.create_subprocess_exec(
                python_bin,
                str(self.runner),
                "--config", str(self.config),
                "--out-dir", str(self.out_dir),
                "--streams", str(self.streams),
                "--preview-width", "160",
                "--jpeg-quality", "42",
                "--preview-fps", os.getenv("MULTI_DISPLAY_FPS", "20.0"),
                "--capture-fps", os.getenv("MULTI_CAPTURE_FPS", "20.0"),
                "--inference-fps", os.getenv("MULTI_INFERENCE_FPS", "15.0"),
                "--input-stride", "1",
                "--imgsz", "640",
                "--inference-workers", "4",
                cwd=str(self.project_root),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            self.last_error = None
            self.started_at = time.monotonic()
            self._stdout_task = asyncio.create_task(self._pump_stream(self.process.stdout, "stdout"))
            self._stderr_task = asyncio.create_task(self._pump_stream(self.process.stderr, "stderr"))
            self._poll_task = asyncio.create_task(self._poll_status_loop())
            self._wait_task = asyncio.create_task(self._wait_for_exit())
            await self._emit("status", "retail.multi.starting", self.status())
            return self.status()

    async def stop(self) -> dict[str, Any]:
        proc = self.process
        if not proc or proc.returncode is not None:
            self.process = None
            self._cancel_background_tasks()
            self._cleanup_outputs()
            return self.status()

        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            self.process = None
            self._cancel_background_tasks()
            self._cleanup_outputs()
            return self.status()

        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()

        self.process = None
        self._cancel_background_tasks()
        self._cleanup_outputs()
        await self._emit("status", "retail.multi.stopped", self.status())
        return self.status()

    def set_display_active(self, active: bool) -> None:
        self._display_active = active
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if active:
            return
        else:
            if self.process and self.process.returncode is None and not (self._stop_task and not self._stop_task.done()):
                self._stop_task = loop.create_task(self.stop())

    def status(self) -> dict[str, Any]:
        physical_running = bool(self.process and self.process.returncode is None)
        stop_pending = bool(self._stop_task and not self._stop_task.done())
        running = physical_running and not (self._display_active and stop_pending)
        status_file = self._read_status()
        if status_file:
            self._latest_status = status_file
        else:
            status_file = self._latest_status or {}
        stream_status = []
        frame_paths = []
        fresh_count = 0
        now = time.time()
        runner_streams = {
            int(item.get("id", 0)): item
            for item in status_file.get("streams", [])
            if isinstance(item, dict)
        }
        for camera_id in range(1, self.streams + 1):
            path = self.frame_path(camera_id)
            runner_stream = runner_streams.get(camera_id, {})
            frame_paths.append(str(path))
            exists = path.is_file()
            size = path.stat().st_size if exists else 0
            mtime = path.stat().st_mtime if exists else 0.0
            fresh = exists and size > 1024 and (now - mtime) < 10.0
            if fresh:
                fresh_count += 1
            stream_status.append({
                "id": camera_id,
                "frame_path": str(path),
                "ready": fresh,
                "size": size,
                "age_seconds": round(now - mtime, 2) if exists else None,
                "stream_url": f"/api/v1/shelf/live/{camera_id}.mjpeg",
                "frame_url": f"/api/v1/shelf/live/{camera_id}.jpg",
                "fps": float(runner_stream.get("display_fps", status_file.get("fps", 0.0)) or 0.0),
                "capture_fps": float(runner_stream.get("capture_fps", 0.0) or 0.0),
                "inference_fps": float(runner_stream.get("inference_fps", 0.0) or 0.0),
                "display_fps": float(runner_stream.get("display_fps", 0.0) or 0.0),
                "encoded": int(runner_stream.get("encoded", 0) or 0),
                "timings": runner_stream.get("timings", {}),
            })

        ready = running and fresh_count == self.streams and bool(status_file.get("running", True))
        if running and ready:
            runtime_status = "READY"
        elif running:
            runtime_status = "BOOTING"
        elif self._prepared:
            runtime_status = "SUSPENDED"
        else:
            runtime_status = "BOOTING"
        display_status = "ACTIVE" if self._display_active else "INACTIVE"
        return {
            "running": running,
            "ready": ready,
            "display_active": self._display_active,
            "display_status": display_status,
            "runtime_status": runtime_status,
            "background_runtime": runtime_status,
            "pid": self.process.pid if running and self.process else None,
            "frame": int(status_file.get("frame", 0) or 0),
            "fps": float(status_file.get("fps", 0.0) or 0.0),
            "total_fps": float(status_file.get("total_fps", 0.0) or 0.0),
            "capture_fps": float(status_file.get("capture_fps", 0.0) or 0.0),
            "inference_fps": float(status_file.get("inference_fps", 0.0) or 0.0),
            "display_fps": float(status_file.get("display_fps", 0.0) or 0.0),
            "streams": stream_status,
            "active_streams": self.streams,
            "frame_paths": frame_paths,
            "last_error": self.last_error,
            "status_path": str(self.status_path()),
            "stream_url": "/api/v1/shelf/live.mjpeg",
            "updated_at": status_file.get("updated_at"),
            "alerts": status_file.get("alerts", []),
            "timeline": status_file.get("timeline", []),
            "resource": status_file.get("resource", {}),
            "optimization": status_file.get("optimization", {}),
        }

    async def is_ready(self) -> bool:
        return bool(self.status()["ready"])

    def frame_path(self, camera_id: int = 1) -> Path:
        return self.out_dir / f"latest_{camera_id}.jpg"

    def status_path(self) -> Path:
        return self.out_dir / "status.json"

    async def subscribe_events(self, max_queue: int = 128) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max_queue)
        self._subscribers.add(queue)
        return queue

    async def unsubscribe_events(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    async def events(self, send_json: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        await self.forward_websocket(send_json)

    async def forward_websocket(self, send_json: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        queue = await self.subscribe_events()
        try:
            await send_json({"type": "connection", "status": "connected", "demo": "multi"})
            await send_json({"type": "retail.multi.status", "data": self.status()})
            while True:
                event = await queue.get()
                await send_json(event)
        finally:
            await self.unsubscribe_events(queue)

    def _cleanup_outputs(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        for name in ("status.json", "stdout.log", "stderr.log"):
            (self.out_dir / name).unlink(missing_ok=True)
        for camera_id in range(1, self.streams + 1):
            (self.out_dir / f"latest_{camera_id}.jpg").unlink(missing_ok=True)

    def _cancel_background_tasks(self) -> None:
        for task in (self._poll_task, self._wait_task, self._stdout_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
        self._poll_task = None
        self._wait_task = None
        self._stdout_task = None
        self._stderr_task = None

    async def _wait_for_exit(self) -> None:
        proc = self.process
        if not proc:
            return
        try:
            await proc.wait()
        finally:
            self.process = None

    async def _pump_stream(self, reader: asyncio.StreamReader | None, stream_name: str) -> None:
        if reader is None:
            return
        log_path = self.out_dir / f"{stream_name}.log"
        with log_path.open("a", encoding="utf-8") as log:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                log.write(line + "\n")
                log.flush()
                if "total_fps" in line or "total_frames" in line or "SUMMARY" in line:
                    await self._emit("status", "retail.multi.log", {"stream": stream_name, "line": line})

    async def _poll_status_loop(self) -> None:
        while True:
            try:
                status = self.status()
                signature = (
                    status["running"],
                    status["ready"],
                    status["frame"],
                    round(float(status["fps"]), 2),
                    tuple((item["id"], item["ready"], item["size"]) for item in status["streams"]),
                    tuple((item.get("camera"), item.get("product"), item.get("status")) for item in status.get("alerts", [])),
                )
                if signature != self._last_signature:
                    self._last_signature = signature
                    await self._emit("status", "retail.multi.status", status)
                    await self._emit("streams", "retail.multi.streams", {"streams": status["streams"]})
                    current_alerts: set[tuple[Any, ...]] = set()
                    for alert in status.get("alerts", []):
                        alert_signature = (
                            alert.get("camera"),
                            alert.get("region_id"),
                            alert.get("timestamp"),
                        )
                        current_alerts.add(alert_signature)
                        if alert_signature not in self._announced_alerts:
                            await self._emit("alert", "stock_alert", alert)
                    self._announced_alerts = current_alerts
                await asyncio.sleep(0.6)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                await self._emit("status", "retail.multi.error", {"error": str(exc)})
                await asyncio.sleep(1.0)

    async def _emit(self, channel: str, event_type: str, payload: dict[str, Any]) -> None:
        event = {
            "type": event_type,
            "channel": channel,
            "timestamp": time.time(),
            "payload": payload,
        }
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def _read_status(self) -> dict[str, Any]:
        path = self.status_path()
        if not path.is_file():
            return {}
        try:
            import json
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}
