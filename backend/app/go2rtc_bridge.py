from __future__ import annotations

import asyncio
import os
import shutil
import signal
import time
from pathlib import Path
from urllib import request as urllib_request


class Go2RtcBridge:
    """Optional local RTSP-to-WebRTC bridge.

    The bridge is deliberately best-effort: inventory capture remains owned by
    RetailSingleAdapter, and MJPEG stays available when go2rtc is absent or
    unavailable.
    """

    def __init__(
        self,
        *,
        source_url: str | None = None,
        binary: str | None = None,
        config_path: Path | None = None,
        api_url: str = "http://127.0.0.1:1984",
    ):
        self.source_url = source_url or os.getenv("RECAMERA_RTSP_URL", "rtsp://192.168.42.1:8554/detected")
        self.binary = binary or os.getenv("GO2RTC_BIN", "go2rtc")
        self.config_path = config_path or Path(os.getenv("GO2RTC_CONFIG", "/tmp/retail-ai-go2rtc.yaml"))
        self.api_url = os.getenv("GO2RTC_API_URL", api_url).rstrip("/")
        self.process: asyncio.subprocess.Process | None = None
        self.last_error: str | None = None
        self.started_at: float | None = None

    @property
    def configured(self) -> bool:
        return bool(shutil.which(self.binary) or Path(self.binary).is_file())

    @property
    def websocket_url(self) -> str:
        base = self.api_url.replace("http://", "ws://").replace("https://", "wss://")
        return f"{base}/api/ws?src=recamera_detected"

    def _write_config(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            "api:\n"
            "  listen: '127.0.0.1:1984'\n"
            "webrtc:\n"
            "  listen: '0.0.0.0:8555'\n"
            "streams:\n"
            "  recamera_detected: " + self.source_url + "\n",
            encoding="utf-8",
        )

    async def start(self) -> dict:
        if self.process and self.process.returncode is None:
            return self.status()
        if not self.configured:
            self.last_error = f"go2rtc binary not found: {self.binary}"
            return self.status()
        self._write_config()
        try:
            self.process = await asyncio.create_subprocess_exec(
                self.binary,
                "-config",
                str(self.config_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            self.started_at = time.time()
            self.last_error = None
        except (OSError, ValueError) as exc:
            self.process = None
            self.last_error = str(exc)
        return self.status()

    async def stop(self) -> dict:
        process = self.process
        if not process or process.returncode is not None:
            self.process = None
            return self.status()
        try:
            os.killpg(process.pid, signal.SIGTERM)
            await asyncio.wait_for(process.wait(), timeout=5)
        except (ProcessLookupError, asyncio.TimeoutError):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
        self.process = None
        return self.status()

    async def health(self) -> bool:
        if not self.process or self.process.returncode is not None:
            return False
        try:
            await asyncio.to_thread(self._fetch, f"{self.api_url}/api/streams")
            return True
        except Exception:
            return False

    @staticmethod
    def _fetch(url: str) -> bytes:
        req = urllib_request.Request(url, headers={"Accept": "application/json"})
        with urllib_request.urlopen(req, timeout=1.5) as response:
            return response.read()

    def status(self) -> dict:
        running = bool(self.process and self.process.returncode is None)
        return {
            "configured": self.configured,
            "running": running,
            "api_url": self.api_url,
            "websocket_url": self.websocket_url,
            "stream_name": "recamera_detected",
            "source_url": self.source_url,
            "pid": self.process.pid if running and self.process else None,
            "started_at": self.started_at,
            "last_error": self.last_error,
        }
