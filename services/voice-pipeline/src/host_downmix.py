#!/usr/bin/env python3
"""stdin: 16kHz S16_LE 双声道 raw → stdout: 16kHz S16_LE 单声道 raw(纯标准库)"""
import os
import json
import math
import queue
import sys
import threading
import time
from pathlib import Path

heartbeat = os.environ.get("CAPTURE_HEARTBEAT")
heartbeat_path = Path(heartbeat) if heartbeat else None
downmix_heartbeat = os.environ.get("DOWNMIX_HEARTBEAT")
downmix_heartbeat_path = Path(downmix_heartbeat) if downmix_heartbeat else None
audio_level = os.environ.get("AUDIO_LEVEL_PATH")
audio_level_path = Path(audio_level) if audio_level else None
capture_bytes = 0
downmix_bytes = 0
last_capture_heartbeat = 0.0
last_downmix_heartbeat = 0.0
level_peak = 0
level_sum_squares = 0
level_samples = 0
dropped_capture_bytes = 0

CAPTURE_BYTES_PER_SECOND = 16000 * 2 * 2
READ_SIZE = 4096
queue_seconds = max(1.0, float(os.environ.get("DOWNMIX_QUEUE_SECONDS", "10")))
audio_queue: queue.Queue[bytes | None] = queue.Queue(
    maxsize=max(16, math.ceil(CAPTURE_BYTES_PER_SECOND * queue_seconds / READ_SIZE))
)
print(
    f"host_downmix version=v2-buffered queue_capacity={audio_queue.maxsize} "
    f"queue_seconds={queue_seconds:g}",
    file=sys.stderr,
    flush=True,
)


def write_heartbeat(path: Path, total: int) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(f"{total}\n", encoding="ascii")
    os.replace(temporary, path)


def write_audio_level(
    path: Path,
    peak: int,
    sum_squares: int,
    samples: int,
    dropped_bytes: int,
) -> None:
    rms = math.sqrt(sum_squares / samples) if samples else 0.0
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(
            {
                "timestamp": time.time(),
                "peak": peak,
                "peak_normalized": round(peak / 32768.0, 6),
                "rms": round(rms, 3),
                "rms_normalized": round(rms / 32768.0, 6),
                "samples": samples,
                "dropped_capture_bytes": dropped_bytes,
                "queue_depth": audio_queue.qsize(),
                "queue_capacity": audio_queue.maxsize,
            },
            separators=(",", ":"),
        ) + "\n",
        encoding="ascii",
    )
    os.replace(temporary, path)


def capture_reader() -> None:
    global capture_bytes, dropped_capture_bytes, last_capture_heartbeat

    while True:
        raw = sys.stdin.buffer.read(READ_SIZE)
        if not raw:
            break
        capture_bytes += len(raw)
        now = time.monotonic()
        if heartbeat_path and now - last_capture_heartbeat >= 0.5:
            try:
                write_heartbeat(heartbeat_path, capture_bytes)
                last_capture_heartbeat = now
            except OSError:
                pass

        while True:
            try:
                audio_queue.put_nowait(raw)
                break
            except queue.Full:
                try:
                    dropped = audio_queue.get_nowait()
                except queue.Empty:
                    continue
                if dropped is not None:
                    dropped_capture_bytes += len(dropped)

    while True:
        try:
            audio_queue.put_nowait(None)
            return
        except queue.Full:
            try:
                dropped = audio_queue.get_nowait()
            except queue.Empty:
                continue
            if dropped is not None:
                dropped_capture_bytes += len(dropped)


threading.Thread(target=capture_reader, name="capture-reader", daemon=True).start()

while True:
    raw = audio_queue.get()
    if raw is None:
        break
    n = len(raw) // 4 * 4
    out = bytearray()
    for i in range(0, n, 4):
        l = int.from_bytes(raw[i:i+2], "little", signed=True)
        r = int.from_bytes(raw[i+2:i+4], "little", signed=True)
        m = (l + r) // 2
        out += m.to_bytes(2, "little", signed=True)
        amplitude = abs(m)
        level_peak = max(level_peak, amplitude)
        level_sum_squares += m * m
        level_samples += 1
    try:
        sys.stdout.buffer.write(out)
        sys.stdout.buffer.flush()
    except BrokenPipeError:
        os._exit(141)
    downmix_bytes += len(out)
    now = time.monotonic()
    if downmix_heartbeat_path and out and now - last_downmix_heartbeat >= 0.5:
        try:
            write_heartbeat(downmix_heartbeat_path, downmix_bytes)
            if audio_level_path:
                write_audio_level(
                    audio_level_path,
                    level_peak,
                    level_sum_squares,
                    level_samples,
                    dropped_capture_bytes,
                )
                level_peak = 0
                level_sum_squares = 0
                level_samples = 0
            last_downmix_heartbeat = now
        except OSError:
            pass
