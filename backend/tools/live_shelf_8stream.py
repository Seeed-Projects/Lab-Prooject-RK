#!/usr/bin/env python3
"""True 4-input RKNN shelf preview.

Four independent VideoCapture inputs are opened from the same demo video. Each
stream is fed through the existing YOLO/RKNN shelf pipeline and writes its own
latest JPEG frame. This is intentionally heavier than the single-preview mode:
it exists to demonstrate actual multi-stream inference behavior.
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
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
import queue
import threading
from typing import Any

import cv2


PROJECT_ROOT = Path(
    os.getenv("MULTI_CAMERA_ROOT", Path(__file__).resolve().parents[2] / "demos" / "multi-camera-shelf")
).expanduser().resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from app.type_mapping import type_labels  # noqa: E402
from runtime.rknn_detector import RKNNDetector  # noqa: E402
from shelf_monitor.drawing import draw_boxes  # noqa: E402
from shelf_monitor.pipeline import ShelfPipeline  # noqa: E402
from shelf_monitor.regions import RegionConfig  # noqa: E402
from shelf_monitor.video_utils import open_video, release  # noqa: E402
from runtime import rknn_detector as rknn_detector_module  # noqa: E402
from shelf_business_events import ShelfEventTracker  # noqa: E402


logger = logging.getLogger("live_shelf_8stream")
STOP = False
PROFILE_CONTEXT = threading.local()
PROFILE_LOCK = threading.Lock()
TIMINGS: dict[str, dict[str, deque[float]]] = defaultdict(
    lambda: defaultdict(lambda: deque(maxlen=2000))
)
COUNTS: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))


def _profile_add(camera: str, stage: str, value_ms: float) -> None:
    with PROFILE_LOCK:
        TIMINGS[camera][stage].append(value_ms)


def _count_add(camera: str, key: str, value: int = 1) -> None:
    with PROFILE_LOCK:
        COUNTS[camera][key] += value


def _median(values) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    mid = len(sorted_values) // 2
    if len(sorted_values) % 2:
        return float(sorted_values[mid])
    return float((sorted_values[mid - 1] + sorted_values[mid]) / 2.0)


def _rolling_fps(timestamps: deque[float]) -> float:
    if len(timestamps) < 2:
        return 0.0
    elapsed = timestamps[-1] - timestamps[0]
    return (len(timestamps) - 1) / elapsed if elapsed > 0 else 0.0


def _install_profilers() -> None:
    original_letterbox_rgb = rknn_detector_module.letterbox_rgb
    original_postprocess_auto = rknn_detector_module.postprocess_auto
    try:
        from rknnlite.api import RKNNLite
        original_inference = RKNNLite.inference
    except Exception:  # pragma: no cover - board-only dependency
        original_inference = None

    def profiled_letterbox_rgb(*args, **kwargs):
        camera = str(getattr(PROFILE_CONTEXT, "camera", "unknown"))
        t0 = time.perf_counter()
        result = original_letterbox_rgb(*args, **kwargs)
        _profile_add(camera, "preprocess_ms", (time.perf_counter() - t0) * 1000)
        return result

    def profiled_postprocess_auto(*args, **kwargs):
        camera = str(getattr(PROFILE_CONTEXT, "camera", "unknown"))
        t0 = time.perf_counter()
        result = original_postprocess_auto(*args, **kwargs)
        _profile_add(camera, "postprocess_ms", (time.perf_counter() - t0) * 1000)
        return result

    def profiled_inference(self, *args, **kwargs):  # noqa: ANN001
        camera = str(getattr(PROFILE_CONTEXT, "camera", "unknown"))
        model = str(getattr(PROFILE_CONTEXT, "model", "unknown"))
        t0 = time.perf_counter()
        result = original_inference(self, *args, **kwargs) if original_inference else None
        _profile_add(f"{camera}:{model}", "inference_ms", (time.perf_counter() - t0) * 1000)
        return result

    rknn_detector_module.letterbox_rgb = profiled_letterbox_rgb
    rknn_detector_module.postprocess_auto = profiled_postprocess_auto
    if original_inference:
        RKNNLite.inference = profiled_inference


def _sample_system_status() -> dict[str, float | int]:
    try:
        import psutil
    except Exception:
        return {}
    proc = psutil.Process()
    children = proc.children(recursive=True)
    mem_mb = proc.memory_info().rss / (1024 * 1024)
    return {
        "cpu_percent": float(psutil.cpu_percent(interval=None)),
        "mem_mb": round(mem_mb, 1),
        "threads": len(proc.threads()),
        "proc_count": len(psutil.pids()),
        "proc_children": len(children),
    }


def _sample_npu() -> dict[str, str | int]:
    npu = Path("/sys/class/devfreq/fdab0000.npu")
    gpu = Path("/sys/class/devfreq/fb000000.gpu-panthor")
    data: dict[str, str | int] = {}
    for name, path in (("npu", npu), ("gpu", gpu)):
        try:
            data[f"{name}_cur_freq"] = int((path / "cur_freq").read_text().strip())
            data[f"{name}_governor"] = (path / "governor").read_text().strip()
        except Exception:
            continue
    return data


def _stock_events(states: list[StreamState]) -> tuple[list[dict], list[dict]]:
    alerts: list[dict] = []
    timeline: list[dict] = []
    for state in states:
        state.event_tracker_alerts, state.event_tracker_timeline = state.event_tracker.snapshot()
        alerts.extend(state.event_tracker_alerts)
        timeline.extend(state.event_tracker_timeline)
    alerts.sort(key=lambda item: item.get("timestamp", 0), reverse=True)
    timeline.sort(key=lambda item: item.get("timestamp", 0), reverse=True)
    return alerts[:8], timeline[:12]


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
        Path(tmp_name).unlink(missing_ok=True)


def atomic_write_json(path: Path, data: dict) -> None:
    atomic_write_bytes(path, json.dumps(data, ensure_ascii=False).encode("utf-8"))


@dataclass
class StreamState:
    index: int
    cap: cv2.VideoCapture
    frame_idx: int
    pipeline: ShelfPipeline
    event_tracker: ShelfEventTracker
    event_tracker_alerts: list[dict] = field(default_factory=list)
    event_tracker_timeline: list[dict] = field(default_factory=list)
    processed: int = 0
    capture_count: int = 0
    display_count: int = 0
    capture_fps_ema: float = 0.0
    inference_fps_ema: float = 0.0
    display_fps_ema: float = 0.0
    capture_previous_wall: float = 0.0
    inference_previous_wall: float = 0.0
    display_previous_wall: float = 0.0
    capture_times: deque[float] = field(default_factory=lambda: deque(maxlen=120))
    inference_times: deque[float] = field(default_factory=lambda: deque(maxlen=120))
    display_times: deque[float] = field(default_factory=lambda: deque(maxlen=120))
    last_encode: float = 0.0
    encoded_frames: int = 0
    last_held_boxes: list[list[float]] | None = None
    last_held_confs: list[float] | None = None
    last_held_labels: list[str] | None = None
    skipped_frames: int = 0
    latest_frame: Any | None = None
    latest_timestamp: float = 0.0
    latest_result: Any | None = None
    latest_detection: DetectionSnapshot | None = None
    latest_frame_version: int = 0
    last_inferred_version: int = -1
    last_displayed_version: int = -1
    last_inference_submit: float = 0.0
    inference_busy: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class WorkerTask:
    frame: object
    version: int
    timestamp: float
    enqueued_at: float


@dataclass(frozen=True)
class DetectionSnapshot:
    boxes: Any
    confs: Any
    labels: list[str]
    timestamp: float
    is_occluded: bool


def capture_frame(
    state: StreamState,
    *,
    info,
    input_stride: int,
):
    camera = f"camera_{state.index + 1:02d}"
    t_capture_total = time.perf_counter()
    t_skip = t_capture_total
    skipped = 0
    for _ in range(max(input_stride - 1, 0)):
        if state.cap.grab():
            state.frame_idx += 1
            skipped += 1
    if skipped:
        _profile_add(camera, "skip_ms", (time.perf_counter() - t_skip) * 1000)
    state.skipped_frames = skipped
    t_capture = time.perf_counter()
    ok, frame = state.cap.read()
    _profile_add(camera, "capture_read_ms", (time.perf_counter() - t_capture) * 1000)
    if not ok:
        t_reset = time.perf_counter()
        state.cap.set(cv2.CAP_PROP_POS_FRAMES, state.index * 9)
        state.frame_idx = state.index * 9
        ok, frame = state.cap.read()
        _profile_add(camera, "capture_reset_ms", (time.perf_counter() - t_reset) * 1000)
        if not ok:
            return False
    _profile_add(camera, "capture_ms", (time.perf_counter() - t_capture_total) * 1000)
    _count_add(camera, "capture_frames")

    timestamp = state.frame_idx / (info.fps or 30.0)
    state.frame_idx += 1
    with state.lock:
        state.latest_frame = frame
        state.latest_timestamp = timestamp
        state.latest_frame_version += 1
    now = time.perf_counter()
    state.capture_previous_wall = now
    state.capture_times.append(now)
    state.capture_fps_ema = _rolling_fps(state.capture_times)
    state.capture_count += 1
    return True


class InferenceWorker(threading.Thread):
    def __init__(
        self,
        state: StreamState,
        *,
        detector: RKNNDetector,
        cfg: dict,
        info,
        region_config: RegionConfig,
        type_config: RegionConfig,
        type_groups: dict,
        type_names: dict,
        task_queue: "queue.Queue[WorkerTask | None]",
    ) -> None:
        super().__init__(daemon=True)
        self.state = state
        self.detector = detector
        self.cfg = cfg
        self.info = info
        self.region_config = region_config
        self.type_config = type_config
        self.type_groups = type_groups
        self.type_names = type_names
        self.task_queue = task_queue

    def run(self) -> None:
        while True:
            task = self.task_queue.get()
            if task is None:
                return
            self._process(task)

    def _process(self, task: WorkerTask) -> None:
        state = self.state
        camera = f"camera_{state.index + 1:02d}"
        with state.lock:
            if state.last_inferred_version == task.version:
                return
            state.inference_busy = True
        _profile_add(camera, "queue_wait_ms", (time.perf_counter() - task.enqueued_at) * 1000)

        PROFILE_CONTEXT.camera = camera
        PROFILE_CONTEXT.model = "shelf"
        t_shelf = time.perf_counter()
        boxes, confs = self.detector.predict(task.frame)
        _profile_add(camera, "rknn_call_ms", (time.perf_counter() - t_shelf) * 1000)
        _count_add(camera, "shelf_inferences")
        PROFILE_CONTEXT.camera = "unknown"
        PROFILE_CONTEXT.model = "unknown"

        result = state.pipeline.process(boxes, confs, task.timestamp)
        t_business = time.perf_counter()
        shelf_labels = type_labels(boxes, self.type_config, self.type_groups, self.type_names)
        state.event_tracker.ingest(result.events, state.pipeline.inventory_state)
        _profile_add(camera, "business_ms", (time.perf_counter() - t_business) * 1000)
        detection = DetectionSnapshot(
            boxes=boxes,
            confs=confs,
            labels=list(shelf_labels),
            timestamp=task.timestamp,
            is_occluded=result.is_occluded,
        )

        with state.lock:
            state.processed += 1
            now = time.perf_counter()
            state.inference_previous_wall = now
            state.inference_times.append(now)
            state.inference_fps_ema = _rolling_fps(state.inference_times)
            state.latest_result = result
            state.latest_detection = detection
            state.last_inferred_version = task.version
            state.inference_busy = False


class CaptureWorker(threading.Thread):
    def __init__(
        self,
        state: StreamState,
        *,
        info,
        input_stride: int,
        capture_fps: float,
        inference_fps: float,
        task_queue: "queue.Queue[WorkerTask | None]",
        stop_event: threading.Event,
    ) -> None:
        super().__init__(daemon=True)
        self.state = state
        self.info = info
        self.input_stride = input_stride
        self.capture_interval = 1.0 / max(capture_fps, 1.0)
        self.inference_interval = 1.0 / max(inference_fps, 1.0)
        self.task_queue = task_queue
        self.stop_event = stop_event

    def run(self) -> None:
        next_tick = time.perf_counter()
        while not self.stop_event.is_set():
            now = time.perf_counter()
            if now < next_tick:
                self.stop_event.wait(next_tick - now)
                continue
            next_tick = max(next_tick + self.capture_interval, now)
            if not capture_frame(self.state, info=self.info, input_stride=self.input_stride):
                continue
            self._submit_latest()

    def _submit_latest(self) -> None:
        state = self.state
        capture_now = time.perf_counter()
        with state.lock:
            if capture_now - state.last_inference_submit < self.inference_interval:
                return
            task = WorkerTask(
                frame=state.latest_frame,
                version=state.latest_frame_version,
                timestamp=state.latest_timestamp,
                enqueued_at=capture_now,
            )
        try:
            while True:
                self.task_queue.get_nowait()
                _count_add(f"camera_{state.index + 1:02d}", "dropped_inference_tasks")
        except queue.Empty:
            pass
        try:
            self.task_queue.put_nowait(task)
            state.last_inference_submit = capture_now
        except queue.Full:
            pass


class DisplayWorker(threading.Thread):
    def __init__(
        self,
        state: StreamState,
        *,
        info,
        region_config: RegionConfig,
        out_dir: Path,
        preview_width: int,
        jpeg_quality: int,
        display_fps: float,
        stop_event: threading.Event,
    ) -> None:
        super().__init__(daemon=True)
        self.state = state
        self.info = info
        self.region_config = region_config
        self.out_dir = out_dir
        self.preview_width = preview_width
        self.jpeg_quality = jpeg_quality
        self.interval = 1.0 / max(display_fps, 1.0)
        self.stop_event = stop_event

    def run(self) -> None:
        next_tick = time.perf_counter()
        while not self.stop_event.is_set():
            now = time.perf_counter()
            if now < next_tick:
                self.stop_event.wait(next_tick - now)
                continue
            next_tick = max(next_tick + self.interval, now)
            self._publish_latest()

    def _publish_latest(self) -> None:
        state = self.state
        camera = f"camera_{state.index + 1:02d}"
        with state.lock:
            frame = state.latest_frame
            version = state.latest_frame_version
            detection = state.latest_detection
            inference_fps = state.inference_fps_ema
        if frame is None or version == state.last_displayed_version:
            return

        t_render = time.perf_counter()
        scale = self.preview_width / frame.shape[1]
        output = cv2.resize(
            frame,
            (self.preview_width, int(frame.shape[0] * scale)),
            interpolation=cv2.INTER_AREA,
        )
        if detection is not None:
            scale_x = output.shape[1] / frame.shape[1]
            scale_y = output.shape[0] / frame.shape[0]
            scaled_boxes = [
                [box[0] * scale_x, box[1] * scale_y, box[2] * scale_x, box[3] * scale_y]
                for box in detection.boxes
            ]
            draw_boxes(output, scaled_boxes, detection.confs, show_conf=False, labels=detection.labels)
        cv2.putText(
            output,
            f"Camera {state.index + 1:02d} TRUE RKNN",
            (5, 18),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.35,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )
        _profile_add(camera, "render_ms", (time.perf_counter() - t_render) * 1000)

        t_encode = time.perf_counter()
        ok, encoded = cv2.imencode(
            ".jpg",
            output,
            [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality],
        )
        _profile_add(camera, "encode_ms", (time.perf_counter() - t_encode) * 1000)
        if not ok:
            return

        atomic_write_bytes(self.out_dir / f"latest_{state.index + 1}.jpg", encoded.tobytes())
        dnow = time.perf_counter()
        with state.lock:
            state.display_previous_wall = dnow
            state.display_times.append(dnow)
            state.display_fps_ema = _rolling_fps(state.display_times)
            state.display_count += 1
            state.encoded_frames += 1
            state.last_encode = dnow
            state.last_displayed_version = version
        _count_add(camera, "encoded_frames")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/runtime.json")
    parser.add_argument("--out-dir", default="/tmp/rk3588_demo_hub/shelf_live")
    parser.add_argument("--streams", type=int, default=4)
    parser.add_argument("--preview-width", type=int, default=288)
    parser.add_argument("--jpeg-quality", type=int, default=62)
    parser.add_argument("--preview-fps", type=float, default=4.0)
    parser.add_argument("--inference-fps", type=float, default=10.0)
    parser.add_argument("--capture-fps", type=float, default=20.0)
    parser.add_argument("--input-stride", type=int, default=2)
    parser.add_argument("--imgsz", type=int, default=0)
    parser.add_argument("--inference-workers", type=int, default=4)
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    _install_profilers()

    out_dir = Path(args.out_dir)
    status_path = out_dir / "status.json"
    cfg = load_json(args.config)
    shelf_cfg = cfg["shelf_model"]
    model_imgsz = int(args.imgsz or shelf_cfg["imgsz"])

    region_config = RegionConfig.load(resolve(cfg["regions"]))
    type_config = RegionConfig.load(resolve(cfg["type_regions"]))
    type_groups = load_json(cfg["type_groups"])
    type_names = load_json(cfg["type_names"])
    first_cap, info = open_video(resolve(cfg["video"]))
    first_cap.release()
    type_config.resolve_for_frame(info.width, info.height)

    event_duration = float(cfg.get("event_duration_seconds", 1.8))
    max_streams = min(max(args.streams, 1), 4)

    states: list[StreamState] = []
    for i in range(max_streams):
        cap, _ = open_video(resolve(cfg["video"]))
        start_frame = i * 9
        cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
        pipeline = ShelfPipeline(region_config, event_duration=event_duration)
        pipeline.resolve(info.width, info.height)
        states.append(StreamState(
            index=i,
            cap=cap,
            frame_idx=start_frame,
            pipeline=pipeline,
            event_tracker=ShelfEventTracker(i + 1),
            capture_previous_wall=time.perf_counter(),
            inference_previous_wall=time.perf_counter(),
            display_previous_wall=time.perf_counter(),
        ))

    logger.info("true 4-stream RKNN preview started; streams=%d out_dir=%s", max_streams, out_dir)
    logger.info(
        "optimization: preview_width=%d preview_fps=%.1f input_stride=%d streams=%d imgsz=%d",
        args.preview_width,
        args.preview_fps,
        args.input_stride,
        max_streams,
        model_imgsz,
    )
    worker_queues: list[queue.Queue[WorkerTask | None]] = []
    workers: list[InferenceWorker] = []
    capture_workers: list[CaptureWorker] = []
    display_workers: list[DisplayWorker] = []
    capture_stop = threading.Event()
    display_stop = threading.Event()
    detector_pool: list[RKNNDetector] = []
    try:
        last_resource_log = 0.0
        worker_count = max(1, min(max_streams, args.inference_workers))
        for worker_idx in range(worker_count):
            detector_pool.append(
                RKNNDetector(
                    resolve(shelf_cfg["path"]),
                    model_imgsz,
                    shelf_cfg["confidence"],
                    shelf_cfg["iou"],
                    cfg.get("core_mask", "auto"),
                )
            )
        for idx, state in enumerate(states):
            worker = InferenceWorker(
                state,
                detector=detector_pool[idx % len(detector_pool)],
                cfg=cfg,
                info=info,
                region_config=region_config,
                type_config=type_config,
                type_groups=type_groups,
                type_names=type_names,
                task_queue=queue.Queue(maxsize=1),
            )
            worker_queues.append(worker.task_queue)
            workers.append(worker)
            worker.start()
        for state, worker_queue in zip(states, worker_queues):
            capture_worker = CaptureWorker(
                state,
                info=info,
                input_stride=args.input_stride,
                capture_fps=args.capture_fps,
                inference_fps=args.inference_fps,
                task_queue=worker_queue,
                stop_event=capture_stop,
            )
            capture_workers.append(capture_worker)
            capture_worker.start()
        for state in states:
            display_worker = DisplayWorker(
                state,
                info=info,
                region_config=region_config,
                out_dir=out_dir,
                preview_width=args.preview_width,
                jpeg_quality=args.jpeg_quality,
                display_fps=args.preview_fps,
                stop_event=display_stop,
            )
            display_workers.append(display_worker)
            display_worker.start()
        last_status_write = 0.0
        status_interval = 0.25
        resource: dict[str, Any] = {}
        while not STOP:
            loop_start = time.perf_counter()
            now_wall = time.time()
            if now_wall - last_resource_log >= 1.0:
                resource = _sample_system_status()
                resource.update(_sample_npu())
                resource["timestamp"] = now_wall
                logger.info("[RESOURCE] %s", json.dumps(resource, ensure_ascii=False))
                last_resource_log = now_wall
            if now_wall - last_status_write < status_interval:
                time.sleep(0.02)
                continue

            stream_status = []
            for state in states:
                camera = f"camera_{state.index + 1:02d}"
                with PROFILE_LOCK:
                    timings = {
                        "capture_ms": round(_median(list(TIMINGS[camera]["capture_ms"])), 2),
                        "preprocess_ms": round(_median(list(TIMINGS[camera]["preprocess_ms"])), 2),
                        "rknn_inference_ms": round(_median(list(TIMINGS[f'{camera}:shelf']["inference_ms"])), 2),
                        "postprocess_ms": round(_median(list(TIMINGS[camera]["postprocess_ms"])), 2),
                        "render_ms": round(_median(list(TIMINGS[camera]["render_ms"])), 2),
                        "encode_ms": round(_median(list(TIMINGS[camera]["encode_ms"])), 2),
                        "queue_wait_ms": round(_median(list(TIMINGS[camera]["queue_wait_ms"])), 2),
                    }
                stream_status.append({
                    "id": state.index + 1,
                    "frame": state.processed,
                    "capture_fps": round(float(state.capture_fps_ema), 2),
                    "inference_fps": round(float(state.inference_fps_ema), 2),
                    "display_fps": round(float(state.display_fps_ema), 2),
                    "encoded": state.encoded_frames,
                    "skipped": state.skipped_frames,
                    "timings": timings,
                    "frame_url": f"/api/v1/shelf/live/{state.index + 1}.jpg",
                    "stream_url": f"/api/v1/shelf/live/{state.index + 1}.mjpeg",
                })
            total_frames = sum(s.processed for s in states)
            total_capture_fps = sum(s.capture_fps_ema for s in states)
            total_infer_fps = sum(s.inference_fps_ema for s in states)
            total_display_fps = sum(s.display_fps_ema for s in states)
            alerts, timeline_entries = _stock_events(states)
            atomic_write_json(status_path, {
                "running": True,
                "mode": "true_8_stream_rknn",
                "streams": stream_status,
                "frame": total_frames,
                "capture_fps": round(float(total_capture_fps), 2),
                "inference_fps": round(float(total_infer_fps), 2),
                "display_fps": round(float(total_display_fps), 2),
                "fps": round(float(total_infer_fps), 2),
                "total_fps": round(float(total_infer_fps), 2),
                "alerts": alerts,
                "timeline": timeline_entries,
                "resource": resource,
                "loop_ms": round((time.perf_counter() - loop_start) * 1000, 1),
                "optimization": {
                    "input_stride": args.input_stride,
                    "capture_target_fps": args.capture_fps,
                    "held_every": 1,
                    "held_stream_limit": 1,
                    "preview_width": args.preview_width,
                    "preview_fps": args.preview_fps,
                    "inference_target_fps": args.inference_fps,
                    "jpeg_quality": args.jpeg_quality,
                    "inference_workers": worker_count,
                    "imgsz": model_imgsz,
                },
                "updated_at": time.time(),
            })
            last_status_write = now_wall
            if total_frames and total_frames % 40 == 0:
                logger.info(
                    "total_frames=%d capture_fps=%.2f inference_fps=%.2f display_fps=%.2f loop_ms=%.1f",
                    total_frames,
                    total_capture_fps,
                    total_infer_fps,
                    total_display_fps,
                    (time.perf_counter() - loop_start) * 1000,
                )
    finally:
        capture_stop.set()
        for capture_worker in capture_workers:
            capture_worker.join(timeout=1.0)
        display_stop.set()
        for display_worker in display_workers:
            display_worker.join(timeout=1.0)
        for worker_queue in worker_queues:
            try:
                while True:
                    worker_queue.get_nowait()
            except queue.Empty:
                worker_queue.put_nowait(None)
        for worker in workers:
            worker.join(timeout=1.0)
        atomic_write_json(status_path, {"running": False, "mode": "true_8_stream_rknn", "updated_at": time.time()})
        for state in states:
            release(state.cap, None)
        for detector in detector_pool:
            try:
                detector.release()
            except Exception:
                logger.exception("failed to release RKNN detector")
        for state in states:
            camera = f"camera_{state.index + 1:02d}"
            summary = {
                "camera": camera,
                "processed": state.processed,
                "encoded": state.encoded_frames,
                "capture_ms_avg": round(_median(TIMINGS[camera]["capture_ms"]), 2),
                "rknn_call_ms_avg": round(_median(TIMINGS[camera]["rknn_call_ms"]), 2),
                "preprocess_ms_avg": round(_median(TIMINGS[camera]["preprocess_ms"]), 2),
                "postprocess_ms_avg": round(_median(TIMINGS[camera]["postprocess_ms"]), 2),
                "inference_shelf_ms_avg": round(_median(TIMINGS[f'{camera}:shelf']["inference_ms"]), 2),
                "render_ms_avg": round(_median(TIMINGS[camera]["render_ms"]), 2),
                "encode_ms_avg": round(_median(TIMINGS[camera]["encode_ms"]), 2),
                "queue_wait_ms_avg": round(_median(TIMINGS[camera]["queue_wait_ms"]), 2),
            }
            logger.info("[SUMMARY] %s", json.dumps(summary, ensure_ascii=False))
        logger.info("true 8-stream RKNN preview stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
