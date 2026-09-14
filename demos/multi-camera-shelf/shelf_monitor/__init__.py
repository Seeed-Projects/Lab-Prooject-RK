"""Retail shelf product counting and restock-alert toolkit.

Public modules
--------------
regions          Shelf ROI configuration, loading and center-point assignment.
inventory        Per-ROI product counting, stock status and pick/restock events.
temporal_filter  Multi-frame count stabilization and event confirmation.
drawing          Annotation / panel rendering for output video.
video_utils      OpenCV VideoCapture / VideoWriter helpers shared by PC and RKNN.
"""

from .regions import RegionConfig, ShelfRegion, assign_detections_to_regions
from .inventory import InventoryState, StockStatus
from .temporal_filter import TemporalStabilizer, InventoryEvent, EventKind
from .pipeline import ShelfPipeline, PipelineResult

__all__ = [
    "RegionConfig",
    "ShelfRegion",
    "assign_detections_to_regions",
    "InventoryState",
    "StockStatus",
    "TemporalStabilizer",
    "InventoryEvent",
    "EventKind",
    "ShelfPipeline",
    "PipelineResult",
]
