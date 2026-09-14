"""ROI-based motion detection for product pickup events.

The YOLO model is trained on products ON the shelf, so a product lifted into
the customer's hand gets NO detection box. Box-based motion therefore misses
the very moment a product is taken. Instead we detect motion directly inside
each shelf ROI via frame differencing: a hand reaching in, or a product being
moved, changes the pixels in that ROI. This is independent of detection boxes
and catches the pickup even when the held product has no box.

A PICKED UP event fires for an ROI when its motion fraction exceeds a
threshold, debounced per ROI.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import cv2
import numpy as np

from .regions import RegionConfig

logger = logging.getLogger("shelf_monitor.motion")


@dataclass
class PickupEvent:
    """A shelf ROI showed significant motion (a product is being handled)."""

    region_id: str
    name: str
    timestamp: float


class MotionPickupDetector:
    """Per-ROI frame-differencing motion detector.

    Parameters
    ----------
    region_config:
        Resolved shelf regions (pixel ROI must be set).
    pixel_threshold:
        Per-pixel absdiff value (0-255) considered "changed".
    motion_fraction:
        Min fraction of changed pixels in an ROI to flag motion.
    cooldown:
        Seconds before the same ROI fires another PICKED UP.
    """

    def __init__(
        self,
        region_config: RegionConfig,
        pixel_threshold: int = 25,
        motion_fraction: float = 0.06,
        cooldown: float = 4.0,
        global_cooldown: float = 2.5,
    ) -> None:
        self.region_config = region_config
        self.pixel_threshold = pixel_threshold
        self.motion_fraction = motion_fraction
        self.cooldown = cooldown
        self.global_cooldown = global_cooldown
        self._prev_gray: np.ndarray | None = None
        self._last_pickup: dict[str, float] = {}
        self._last_any: float = -1e9

    def update(self, frame_bgr: np.ndarray, timestamp: float) -> list[PickupEvent]:
        """Process one frame; return pickup events that fired this frame."""
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (21, 21), 0)
        events: list[PickupEvent] = []
        if self._prev_gray is None or self._prev_gray.shape != gray.shape:
            self._prev_gray = gray
            return events

        diff = cv2.absdiff(gray, self._prev_gray)
        motion_mask = diff > self.pixel_threshold

        # global cooldown: after any pickup, suppress all regions briefly so a
        # single hand sweep across the shelf yields one event, not four.
        if (timestamp - self._last_any) < self.global_cooldown:
            self._prev_gray = gray
            return events

        for reg in self.region_config.regions:
            x1, y1, x2, y2 = reg.pixel_roi
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(gray.shape[1], x2), min(gray.shape[0], y2)
            if x2 <= x1 or y2 <= y1:
                continue
            frac = float(motion_mask[y1:y2, x1:x2].mean())
            if frac >= self.motion_fraction:
                last = self._last_pickup.get(reg.id, -1e9)
                if (timestamp - last) >= self.cooldown:
                    events.append(PickupEvent(reg.id, reg.name, timestamp))
                    self._last_pickup[reg.id] = timestamp
                    self._last_any = timestamp
                    break  # one event per frame (the first moving region)
        self._prev_gray = gray
        return events
