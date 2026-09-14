#!/usr/bin/env python3
"""RK3588 dual-RKNN video inference matching the current PC presentation."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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
from shelf_monitor.video_utils import make_writer, open_video, release  # noqa: E402

logger = logging.getLogger("infer_video_rknn")


def resolve(path: str | Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else ROOT / path


def load_json(path: str | Path) -> dict:
    return json.loads(resolve(path).read_text(encoding="utf-8"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/runtime.json"))
    parser.add_argument("--check-config", action="store_true", help="Validate paths/config without loading RKNN")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def validate_config(cfg: dict, require_models: bool) -> list[str]:
    errors: list[str] = []
    required = ["video", "regions", "type_regions", "type_groups", "type_names", "scripted_events"]
    for key in required:
        if key not in cfg:
            errors.append(f"missing config key: {key}")
        elif not resolve(cfg[key]).exists():
            errors.append(f"missing file for {key}: {resolve(cfg[key])}")
    for model_key in ("shelf_model", "held_model"):
        model_cfg = cfg.get(model_key, {})
        if not {"path", "imgsz", "confidence", "iou"}.issubset(model_cfg):
            errors.append(f"incomplete {model_key} config")
        elif require_models and not resolve(model_cfg["path"]).exists():
            errors.append(f"missing RKNN model: {resolve(model_cfg['path'])}")
    return errors


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    cfg = load_json(args.config)
    errors = validate_config(cfg, require_models=not args.check_config)
    if errors:
        for error in errors:
            logger.error(error)
        return 2
    if args.check_config:
        logger.info("configuration is valid; RKNN model presence was not required")
        return 0

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
    writer = make_writer(resolve(cfg["output"]), info.width, info.height, info.fps or 30.0, cfg.get("codec", "mp4v"))
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

    frame_idx = 0
    processed = 0
    fps_ema = 0.0
    previous_wall = time.perf_counter()
    started = previous_wall
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
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
                recent_events.append(
                    InventoryEvent("__demo__", "Pickup detected", EventKind.REMOVED, amount, timestamp)
                )
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
            writer.write(output)
            processed += 1

            now = time.perf_counter()
            instantaneous = 1.0 / max(1e-6, now - previous_wall)
            previous_wall = now
            fps_ema = 0.9 * fps_ema + 0.1 * instantaneous if fps_ema else instantaneous
            if processed % 30 == 0:
                logger.info("frame=%d time=%.1fs fps=%.1f", processed, timestamp, fps_ema)
    finally:
        release(cap, writer)
        shelf_detector.release()
        held_detector.release()
    elapsed = time.perf_counter() - started
    logger.info("complete: %d frames, %.2fs, average %.1f FPS", processed, elapsed, processed / max(elapsed, 1e-6))
    logger.info("output: %s", resolve(cfg["output"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

