#!/usr/bin/env python3
"""Persistent single-owner proxy for reCamera's detected RTSP stream."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import cv2

STOP = False


def on_signal(signum, frame):  # noqa: ARG001
    global STOP
    STOP = True


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        Path(tmp_name).replace(path)
    finally:
        Path(tmp_name).unlink(missing_ok=True)


def atomic_write_json(path: Path, data: dict) -> None:
    atomic_write(path, json.dumps(data, ensure_ascii=False).encode("utf-8"))


def network_reachable(host: str) -> bool:
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", "1", host],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def port_ready(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except OSError:
        return False


def open_capture(url: str) -> cv2.VideoCapture:
    os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|stimeout;5000000")
    params = []
    if hasattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC"):
        params.extend([cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000])
    if hasattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC"):
        params.extend([cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000])
    try:
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG, params)
    except (TypeError, cv2.error):
        cap = cv2.VideoCapture(url)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rtsp-url", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--idle-fps", type=float, default=1.0)
    parser.add_argument("--active-fps", type=float, default=15.0)
    parser.add_argument("--jpeg-quality", type=int, default=82)
    parser.add_argument("--first-connect-grace", type=float, default=360.0)
    parser.add_argument("--retry-interval", type=float, default=10.0)
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    parsed = urlparse(args.rtsp_url)
    host = parsed.hostname or "192.168.42.1"
    port = parsed.port or 8554
    out_dir = Path(args.out_dir)
    status_path = out_dir / "prewarm_status.json"
    frame_path = out_dir / "latest.jpg"
    active_path = out_dir / "capture_active"
    cap: cv2.VideoCapture | None = None
    frames = 0
    connection_attempts = 0
    started_at = time.time()
    fps_started = time.monotonic()
    fps_frames = 0
    capture_fps = 0.0
    last_error: str | None = None
    last_attempt = 0.0
    network_ok = False
    rtsp_ok = False

    def publish(connected: bool, phase: str) -> None:
        waiting_seconds = max(0.0, time.time() - started_at)
        within_grace = not connected and waiting_seconds <= args.first_connect_grace
        atomic_write_json(status_path, {
            "running": True,
            "connected": connected,
            "active": active_path.exists(),
            "frames": frames,
            "fps": round(capture_fps, 2),
            "source_url": args.rtsp_url,
            "network_reachable": network_ok,
            "rtsp_port_ready": rtsp_ok,
            "phase": phase,
            "connection_attempts": connection_attempts,
            "waiting_seconds": round(waiting_seconds, 1),
            "first_connect_grace_seconds": args.first_connect_grace,
            "retry_interval_seconds": args.retry_interval,
            "last_error": None if within_grace else last_error,
            "updated_at": time.time(),
        })

    try:
        while not STOP:
            if cap is None or not cap.isOpened():
                if time.monotonic() - last_attempt < args.retry_interval:
                    time.sleep(0.1)
                    continue
                last_attempt = time.monotonic()
                connection_attempts += 1
                ping_ok = network_reachable(host)
                rtsp_ok = port_ready(host, port)
                network_ok = ping_ok or rtsp_ok
                if not network_ok:
                    last_error = "reCamera network is not reachable"
                    publish(False, "WAITING_FOR_RECAMERA")
                    continue
                if not rtsp_ok:
                    last_error = "reCamera RTSP service is not ready"
                    publish(False, "CONNECTING_TO_STREAM")
                    continue
                publish(False, "CONNECTING_TO_STREAM")
                cap = open_capture(args.rtsp_url)
                if not cap.isOpened():
                    last_error = "reCamera detected stream is not ready"
                    publish(False, "CONNECTING_TO_STREAM")
                    cap.release()
                    cap = None
                    continue

            frame_started = time.monotonic()
            ok, frame = cap.read()
            if not ok or frame is None:
                last_error = "reCamera detected stream frame read failed"
                cap.release()
                cap = None
                publish(False, "CONNECTING_TO_STREAM")
                continue

            encoded, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, args.jpeg_quality])
            if not encoded:
                last_error = "reCamera frame JPEG encoding failed"
                publish(False, "CONNECTING_TO_STREAM")
                continue
            atomic_write(frame_path, jpeg.tobytes())
            frames += 1
            fps_frames += 1
            elapsed = time.monotonic() - fps_started
            if elapsed >= 1.0:
                capture_fps = fps_frames / elapsed
                fps_frames = 0
                fps_started = time.monotonic()
            last_error = None
            network_ok = True
            rtsp_ok = True
            publish(True, "STREAMING")

            target_fps = args.active_fps if active_path.exists() else args.idle_fps
            interval = 1.0 / max(target_fps, 0.1)
            time.sleep(max(0.0, interval - (time.monotonic() - frame_started)))
    finally:
        if cap is not None:
            cap.release()
        atomic_write_json(status_path, {
            "running": False,
            "connected": False,
            "frames": frames,
            "fps": 0.0,
            "source_url": args.rtsp_url,
            "network_reachable": network_ok,
            "rtsp_port_ready": rtsp_ok,
            "phase": "SUSPENDED",
            "connection_attempts": connection_attempts,
            "waiting_seconds": round(max(0.0, time.time() - started_at), 1),
            "first_connect_grace_seconds": args.first_connect_grace,
            "retry_interval_seconds": args.retry_interval,
            "last_error": last_error,
            "updated_at": time.time(),
        })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
