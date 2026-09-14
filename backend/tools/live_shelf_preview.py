#!/usr/bin/env python3
"""Low-overhead live preview runner for the RK3588 shelf demo.

This is intentionally an adapter-side runner, not a rewrite of the demo. It
reuses the existing RKNNDetector, ShelfPipeline and drawing modules, but emits a
browser-friendly latest-frame preview while inference is running.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

import cv2


PROJECT_ROOT = Path(
    os.getenv("MULTI_CAMERA_ROOT", Path(__file__).resolve().parents[2] / "demos" / "multi-camera-shelf")
).expanduser().resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from app.tiled_inference import detect_tiled  # noqa: E402
from app.type_mapping import HeldTypeTracker, type_labels  # noqa: E402
from runtime.rknn_detector import RKNNDetector  # noqa: E402
from shelf_monitor.drawing import (  # noqa: E402
    draw_boxes,
    draw_events,
    draw_inventory_panel,
    draw_low_stock,
    draw_regions,
)
from shelf_monitor.pipeline import ShelfPipeline  # noqa: E402
from shelf_monitor.regions import RegionConfig  # noqa: E402
from shelf_monitor.temporal_filter import EventKind, InventoryEvent  # noqa: E402
from shelf_monitor.video_utils import open_video, release  # noqa: E402


logger = logging.getLogger("live_shelf_preview")
STOP = False


def on_signal(signum, frame):  # noqa: ARG001
    global STOP
    STOP = True


def resolve(path: str | Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_json(path: str | Path) -> dict:
    return json.loads(resolve(path).read_text(encoding="utf-8"))


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        Path(tmp_name).replace(path)
    finally:
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except Exception:
            pass


def atomic_write_json(path: Path, data: dict) -> None:
    atomic_write_bytes(path, json.dumps(data, ensure_ascii=False).encode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/runtime.json")
    parser.add_argument("--out-dir", default="/tmp/rk3588_demo_hub/shelf_live")
    parser.add_argument("--preview-width", type=int, default=360)
    parser.add_argument("--jpeg-quality", type=int, default=72)
    parser.add_argument("--preview-fps", type=float, default=6.0)
    parser.add_argument("--loop", action="store_true", default=True)
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    out_dir = Path(args.out_dir)
    frame_path = out_dir / "latest.jpg"
    status_path = out_dir / "status.json"
    cfg = load_json(args.config)
    logger.info("live preview starting; out_dir=%s", out_dir)

    shelf_cfg = cfg["shelf_model"]
    held_cfg = cfg["held_model"]
    shelf_detector = RKNNDetector(
        resolve(shelf_cfg["path"]), shelf_cfg["imgsz"], shelf_cfg["confidence"],
        shelf_cfg["iou"], cfg.get("core_mask", "auto"),
    )
    held_detector = RKNNDetector(
        resolve(held_cfg["path"]), held_cfg["imgsz"], held_cfg["confidence"],
        held_cfg["iou"], cfg.get("core_mask", "auto"),
    )

    region_config = RegionConfig.load(resolve(cfg["regions"]))
    type_config = RegionConfig.load(resolve(cfg["type_regions"]))
    type_groups = load_json(cfg["type_groups"])
    type_names = load_json(cfg["type_names"])
    event_cfg = load_json(cfg["scripted_events"])
    cap, info = open_video(resolve(cfg["video"]))
    pipeline = ShelfPipeline(region_config, event_duration=float(cfg.get("event_duration_seconds", 1.8)))
    pipeline.resolve(info.width, info.height)
    type_config.resolve_for_frame(info.width, info.height)
    held_type_tracker = HeldTypeTracker(type_config, type_groups, type_names)

    timeline = [(float(item["time_seconds"]), int(item["removed"])) for item in event_cfg["events"]]
    active_windows = [(float(a), float(b)) for a, b in held_cfg.get("active_windows", [])]
    scripted_counts = {region.id: region.capacity for region in region_config.regions}
    region_order = list(cfg.get("scripted_region_order", []))
    fired: set[int] = set()
    recent_events: list[InventoryEvent] = []
    event_duration = float(cfg.get("event_duration_seconds", 1.8))
    encode_interval = 1.0 / max(args.preview_fps, 1.0)
    last_encode = 0.0
    frame_idx = 0
    processed = 0
    fps_ema = 0.0
    previous_wall = time.perf_counter()
    started = previous_wall

    try:
        while not STOP:
            ok, frame = cap.read()
            if not ok:
                if args.loop:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    frame_idx = 0
                    fired.clear()
                    recent_events.clear()
                    scripted_counts = {region.id: region.capacity for region in region_config.regions}
                    continue
                break
            timestamp = frame_idx / (info.fps or 30.0)
            frame_idx += 1
            boxes, confs = shelf_detector.predict(frame)
            result = pipeline.process(boxes, confs, timestamp)

            held_boxes: list[list[float]] = []
            held_confs: list[float] = []
            held_active = not active_windows or any(start <= timestamp <= end for start, end in active_windows)
            if held_active:
                held_boxes, held_confs = detect_tiled(
                    held_detector,
                    frame,
                    tile_size=int(held_cfg.get("imgsz", 320)),
                    overlap=int(held_cfg.get("tile_overlap", 80)),
                    nms_iou=float(held_cfg.get("iou", 0.35)),
                )

            shelf_labels = type_labels(boxes, type_config, type_groups, type_names)
            held_labels = held_type_tracker.update(held_boxes, timestamp)
            for event_idx, (event_time, amount) in enumerate(timeline):
                if event_idx in fired or timestamp < event_time:
                    continue
                recent_events.append(InventoryEvent("__demo__", "Pickup detected", EventKind.REMOVED, amount, timestamp))
                if region_order:
                    region_id = region_order[min(event_idx, len(region_order) - 1)]
                    if region_id in scripted_counts:
                        scripted_counts[region_id] = max(0, scripted_counts[region_id] - amount)
                fired.add(event_idx)
            recent_events = [event for event in recent_events if timestamp - event.timestamp <= event_duration]
            pipeline.inventory_state.update(scripted_counts, recent_events)

            output = frame.copy()
            draw_regions(output, region_config)
            draw_boxes(output, boxes, confs, show_conf=False, labels=shelf_labels)
            for box, score, label in zip(held_boxes, held_confs, held_labels):
                x1, y1, x2, y2 = (int(round(value)) for value in box)
                cv2.rectangle(output, (x1, y1), (x2, y2), (0, 255, 255), 2)
                text = f"{label} HELD {score:.2f}" if label else f"HELD {score:.2f}"
                cv2.putText(output, text, (x1, max(14, y1 - 4)), cv2.FONT_HERSHEY_SIMPLEX,
                            0.42, (0, 255, 255), 1, cv2.LINE_AA)
            draw_inventory_panel(output, pipeline.inventory_state, fps_ema or info.fps, timestamp, result.is_occluded)
            draw_events(output, recent_events)
            draw_low_stock(output, pipeline.inventory_state)

            processed += 1
            now = time.perf_counter()
            instantaneous = 1.0 / max(1e-6, now - previous_wall)
            previous_wall = now
            fps_ema = 0.9 * fps_ema + 0.1 * instantaneous if fps_ema else instantaneous

            if now - last_encode >= encode_interval:
                scale = args.preview_width / output.shape[1]
                tile = cv2.resize(output, (args.preview_width, int(output.shape[0] * scale)), interpolation=cv2.INTER_AREA)
                tiles = []
                for idx in range(8):
                    item = tile.copy()
                    cv2.putText(item, f"Camera {idx + 1:02d}", (12, 30), cv2.FONT_HERSHEY_SIMPLEX,
                                0.72, (255, 255, 255), 2, cv2.LINE_AA)
                    cv2.putText(item, f"{fps_ema:.1f} FPS", (12, 62), cv2.FONT_HERSHEY_SIMPLEX,
                                0.58, (180, 239, 123), 2, cv2.LINE_AA)
                    tiles.append(item)
                preview = cv2.vconcat([
                    cv2.hconcat(tiles[0:4]),
                    cv2.hconcat(tiles[4:8]),
                ])
                ok, encoded = cv2.imencode(".jpg", preview, [int(cv2.IMWRITE_JPEG_QUALITY), args.jpeg_quality])
                if ok:
                    atomic_write_bytes(frame_path, encoded.tobytes())
                    last_encode = now
            status = {
                "running": True,
                "frame": processed,
                "source_frame": frame_idx,
                "fps": round(float(fps_ema), 2),
                "total_fps_display": round(float(fps_ema) * 8, 2),
                "timestamp": round(timestamp, 2),
                "preview": str(frame_path),
                "updated_at": time.time(),
            }
            atomic_write_json(status_path, status)
            if processed % 15 == 0:
                logger.info("frame=%d time=%.1fs fps=%.1f preview=%s", processed, timestamp, fps_ema, frame_path)
    finally:
        atomic_write_json(status_path, {
            "running": False,
            "frame": processed,
            "fps": round(float(fps_ema), 2),
            "preview": str(frame_path),
            "updated_at": time.time(),
        })
        release(cap, None)
        shelf_detector.release()
        held_detector.release()
        logger.info("live preview stopped: %d frames, average %.1f FPS", processed, processed / max(time.perf_counter() - started, 1e-6))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
