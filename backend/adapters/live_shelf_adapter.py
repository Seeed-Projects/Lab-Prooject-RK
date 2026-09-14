from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import Any


class LiveShelfAdapter:
    def __init__(
        self,
        runner: Path | None = None,
        out_dir: Path = Path("/tmp/rk3588_demo_hub/shelf_live"),
        inference_root: Path | None = None,
    ):
        backend_root = Path(__file__).resolve().parents[1]
        self.runner = runner or backend_root / "tools" / "live_shelf_8stream.py"
        self.inference_root = inference_root or backend_root.parent / "demos" / "multi-camera-shelf"
        self.out_dir = out_dir
        self.process: asyncio.subprocess.Process | None = None
        self.last_error: str | None = None

    async def start(self) -> dict[str, Any]:
        if self.process and self.process.returncode is None:
            return self.status()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        for item in self.out_dir.glob("latest*.jpg"):
            item.unlink(missing_ok=True)
        (self.out_dir / "status.json").unlink(missing_ok=True)
        env = dict(os.environ)
        env.setdefault("PYTHONUNBUFFERED", "1")
        env["PYTHONPATH"] = str(self.inference_root)
        python_bin = str(self.inference_root / ".venv" / "bin" / "python")
        log_path = self.out_dir / "runner.log"
        log_file = log_path.open("ab")
        self.process = await asyncio.create_subprocess_exec(
            python_bin,
            str(self.runner),
            "--out-dir",
            str(self.out_dir),
            "--preview-width",
            "288",
            "--preview-fps",
            "4",
            "--input-stride",
            "2",
            "--held-every",
            "6",
            "--held-stream-limit",
            "2",
            cwd=str(self.inference_root),
            env=env,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )
        self.last_error = None
        return self.status()

    async def stop(self) -> dict[str, Any]:
        if not self.process or self.process.returncode is not None:
            self.process = None
            return self.status()
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            self.process = None
            return self.status()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=3)
        except asyncio.TimeoutError:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await self.process.wait()
        self.process = None
        return self.status()

    def status(self) -> dict[str, Any]:
        status = self._read_status()
        running = bool(self.process and self.process.returncode is None)
        status.update({
            "running": running,
            "pid": self.process.pid if running and self.process else None,
            "frame_url": "/api/v1/shelf/live.jpg",
            "stream_url": "/api/v1/shelf/live.mjpeg",
            "last_error": self.last_error,
        })
        return status

    def frame_path(self) -> Path:
        return self.out_dir / "latest.jpg"

    def _read_status(self) -> dict[str, Any]:
        path = self.out_dir / "status.json"
        if not path.is_file():
            return {"frame": 0, "fps": 0, "total_fps_display": 0}
        try:
            import json
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}
