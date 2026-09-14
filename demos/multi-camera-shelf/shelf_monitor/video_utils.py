"""OpenCV video capture / writer helpers shared by the PC and RK3576 pipelines.

Centralizing this keeps error handling consistent (clear messages on missing
files, unreadable videos, writer failures) and guarantees both pipelines
release resources correctly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import cv2

logger = logging.getLogger("shelf_monitor.video_utils")


@dataclass
class VideoInfo:
    """Basic properties of an opened video."""

    path: str
    width: int
    height: int
    fps: float
    frame_count: int

    @property
    def duration(self) -> float:
        return (self.frame_count / self.fps) if self.fps > 0 else 0.0


def open_video(path: str | Path) -> tuple[cv2.VideoCapture, VideoInfo]:
    """Open a video file and return (capture, info).

    Raises FileNotFoundError if the file is missing and RuntimeError if OpenCV
    cannot open it. Modern OpenCV (>=4.5) applies rotation display matrices
    automatically, so width/height reflect the displayed (de-rotated) frame.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"video file not found: {p}")
    cap = cv2.VideoCapture(str(p))
    if not cap.isOpened():
        raise RuntimeError(f"could not open video: {p}")
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if width <= 0 or height <= 0:
        cap.release()
        raise RuntimeError(f"could not read video dimensions: {p}")
    info = VideoInfo(str(p), width, height, fps, frame_count)
    logger.info("opened video %s: %dx%d @ %.3f fps, %d frames (%.2fs)",
                p.name, width, height, fps, frame_count, info.duration)
    return cap, info


def make_writer(path: str | Path, width: int, height: int, fps: float,
                codec: str = "mp4v") -> cv2.VideoWriter:
    """Create a VideoWriter for an MP4 output.

    Raises RuntimeError on failure (e.g. unsupported codec / bad path).
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fourcc = cv2.VideoWriter_fourcc(*codec)
    writer = cv2.VideoWriter(str(p), fourcc, fps, (int(width), int(height)))
    if not writer.isOpened():
        raise RuntimeError(f"VideoWriter failed to open for {p} (codec={codec})")
    logger.info("writing output to %s (%dx%d @ %.3f fps, codec=%s)",
                p, width, height, fps, codec)
    return writer


def release(cap: cv2.VideoCapture | None, writer: cv2.VideoWriter | None) -> None:
    """Safely release capture and writer, ignoring None."""
    try:
        if cap is not None:
            cap.release()
    except Exception:  # pragma: no cover - defensive
        logger.exception("failed to release VideoCapture")
    try:
        if writer is not None:
            writer.release()
    except Exception:  # pragma: no cover - defensive
        logger.exception("failed to release VideoWriter")
