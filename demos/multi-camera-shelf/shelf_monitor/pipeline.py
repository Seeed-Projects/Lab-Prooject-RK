"""Shared per-frame pipeline: detection -> ROI assignment -> stabilization -> inventory.

Encapsulating this here means the PC script (``scripts/infer_video.py``), the
RK3576 script (``rknn/infer_video_rknn.py``) and any test/demo driver all use
*identical* counting, stabilization and event logic. They only differ in how
the raw ``product`` detection boxes are produced.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Sequence

from .inventory import InventoryState
from .regions import AssignmentResult, RegionConfig, assign_detections_to_regions
from .temporal_filter import InventoryEvent, TemporalStabilizer

logger = logging.getLogger("shelf_monitor.pipeline")


@dataclass
class PipelineResult:
    """Everything a renderer needs for one frame."""

    assignment: AssignmentResult
    confirmed_counts: dict[str, int]
    events: list[InventoryEvent]  # events that fired this frame
    recent_events: list[InventoryEvent]  # events within event_duration of now
    is_occluded: bool
    phase: str
    raw_counts: dict[str, int]
    total: int


class ShelfPipeline:
    """Stateful per-frame processor tying regions, stabilizer and inventory."""

    def __init__(
        self,
        region_config: RegionConfig,
        history_size: int = 15,
        confirmation_frames: int = 10,
        occlusion_recovery_frames: int = 15,
        occlusion_drop_ratio: float = 0.30,
        event_duration: float = 2.0,
        event_persist_seconds: float = 2.0,
        allow_over_capacity: bool = False,
    ) -> None:
        self.region_config = region_config
        self.stabilizer = TemporalStabilizer(
            region_config,
            history_size=history_size,
            confirmation_frames=confirmation_frames,
            occlusion_recovery_frames=occlusion_recovery_frames,
            occlusion_drop_ratio=occlusion_drop_ratio,
            event_duration=event_duration,
            event_persist_seconds=event_persist_seconds,
            allow_over_capacity=allow_over_capacity,
        )
        self.inventory = InventoryState(region_config)
        self.event_duration = event_duration
        self._all_events: list[InventoryEvent] = []
        self._resolved = False

    def resolve(self, width: int, height: int) -> None:
        """Resolve ROI pixel coordinates for the given frame size."""
        self.region_config.resolve_for_frame(width, height)
        self._resolved = True

    def process(
        self,
        boxes: Sequence[Sequence[float]],
        confs: Sequence[float],
        timestamp: float,
    ) -> PipelineResult:
        """Process one frame's detections and return the render-ready state.

        Parameters
        ----------
        boxes:
            Product detection boxes [x1, y1, x2, y2] in absolute frame pixels.
            The caller is responsible for class/confidence filtering so that
            only ``product`` boxes are passed in.
        confs:
            Confidence for each box (parallel to boxes).
        timestamp:
            Current video time in seconds.
        """
        if not self._resolved:
            raise RuntimeError("ShelfPipeline.resolve(width, height) must be called first")

        assignment = assign_detections_to_regions(boxes, self.region_config)
        stab = self.stabilizer.update(assignment.counts, timestamp)
        self.inventory.update(stab.confirmed_counts, stab.events)
        self._all_events.extend(stab.events)
        recent = [e for e in self._all_events if (timestamp - e.timestamp) <= self.event_duration]

        return PipelineResult(
            assignment=assignment,
            confirmed_counts=stab.confirmed_counts,
            events=stab.events,
            recent_events=recent,
            is_occluded=stab.is_occluded,
            phase=stab.phase,
            raw_counts=stab.raw_counts,
            total=self.inventory.total(),
        )

    @property
    def inventory_state(self) -> InventoryState:
        return self.inventory
