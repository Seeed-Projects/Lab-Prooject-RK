"""Shelf ROI configuration and detection-to-region assignment.

The YOLO model only detects a generic ``product``. Product identity is
resolved geometrically: each detection box's center point is matched against
fixed shelf regions of interest (ROIs) defined in ``shelf_regions.json``.

This module is shared verbatim by the PC pipeline (``scripts/infer_video.py``)
and the RK3576 pipeline (``rknn/infer_video_rknn.py``) so that both use
identical ROI logic.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

logger = logging.getLogger("shelf_monitor.regions")


@dataclass(frozen=True)
class ShelfRegion:
    """A single shelf region of interest mapped to one product type.

    ``roi`` is stored in the coordinate mode from the JSON file
    (``normalized`` 0..1 or ``pixel``). ``pixel_roi`` is the resolved
    [x1, y1, x2, y2] in absolute frame pixels, filled in by
    :meth:`RegionConfig.resolve_for_frame`.
    """

    id: str
    name: str
    roi: tuple[float, float, float, float]
    capacity: int
    low_stock_threshold: int
    coordinate_mode: str = "normalized"
    pixel_roi: tuple[int, int, int, int] = field(default=(0, 0, 0, 0))

    def contains_center(self, cx: float, cy: float) -> bool:
        """Return True if point (cx, cy) lies inside this region's pixel ROI."""
        x1, y1, x2, y2 = self.pixel_roi
        return (x1 <= cx <= x2) and (y1 <= cy <= y2)


@dataclass
class RegionConfig:
    """Parsed ``shelf_regions.json`` content."""

    coordinate_mode: str
    baseline_mode: str
    baseline_seconds: float
    regions: list[ShelfRegion]

    @classmethod
    def load(cls, path: str | Path) -> "RegionConfig":
        """Load and validate a shelf_regions.json file."""
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"shelf regions config not found: {p}")
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict) -> "RegionConfig":
        coord_mode = str(data.get("coordinate_mode", "normalized")).lower()
        if coord_mode not in {"normalized", "pixel"}:
            raise ValueError(f"unknown coordinate_mode: {coord_mode!r}")
        baseline_mode = str(data.get("baseline_mode", "config")).lower()
        if baseline_mode not in {"auto", "config"}:
            raise ValueError(f"unknown baseline_mode: {baseline_mode!r}")
        regions: list[ShelfRegion] = []
        for r in data.get("regions", []):
            roi = tuple(float(v) for v in r["roi"])
            if len(roi) != 4:
                raise ValueError(f"region {r.get('id')!r} roi must have 4 values")
            regions.append(
                ShelfRegion(
                    id=str(r["id"]),
                    name=str(r.get("name", r["id"])),
                    roi=roi,  # type: ignore[arg-type]
                    capacity=int(r.get("capacity", 0)),
                    low_stock_threshold=int(r.get("low_stock_threshold", 0)),
                    coordinate_mode=coord_mode,
                )
            )
        if not regions:
            raise ValueError("shelf_regions.json defines no regions")
        return cls(
            coordinate_mode=coord_mode,
            baseline_mode=baseline_mode,
            baseline_seconds=float(data.get("baseline_seconds", 2.0)),
            regions=regions,
        )

    def resolve_for_frame(self, width: int, height: int) -> "RegionConfig":
        """Convert every region's ROI to absolute pixel coordinates in-place.

        Returns ``self`` for chaining. For ``normalized`` mode the ROI values
        are scaled by (width, height). For ``pixel`` mode they are clamped to
        the frame and used as-is.
        """
        for reg in self.regions:
            x1, y1, x2, y2 = reg.roi
            if reg.coordinate_mode == "normalized":
                px1 = int(round(x1 * width))
                py1 = int(round(y1 * height))
                px2 = int(round(x2 * width))
                py2 = int(round(y2 * height))
            else:
                px1, py1, px2, py2 = (int(round(v)) for v in (x1, y1, x2, y2))
            # clamp + ensure x1<x2, y1<y2
            px1, px2 = sorted((max(0, px1), min(width, px2)))
            py1, py2 = sorted((max(0, py1), min(height, py2)))
            object.__setattr__(reg, "pixel_roi", (px1, py1, px2, py2))
        return self

    def get(self, region_id: str) -> ShelfRegion | None:
        for reg in self.regions:
            if reg.id == region_id:
                return reg
        return None


@dataclass
class AssignmentResult:
    """Outcome of assigning a batch of detections to regions."""

    per_region: dict[str, list[int]]  # region_id -> list of detection indices
    unassigned: list[int]
    counts: dict[str, int]


def box_center(box: tuple[float, float, float, float]) -> tuple[float, float]:
    """Return the center (cx, cy) of a [x1, y1, x2, y2] box."""
    x1, y1, x2, y2 = box
    return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)


def assign_detections_to_regions(
    boxes: Iterable[tuple[float, float, float, float]],
    config: RegionConfig,
) -> AssignmentResult:
    """Assign each detection box to at most one region by center point.

    A box is counted in the first region whose pixel ROI contains its center.
    Boxes whose center lies outside every region are reported as unassigned
    (and logged) but not counted.

    Parameters
    ----------
    boxes:
        Iterable of [x1, y1, x2, y2] in absolute frame pixels. The caller must
        have called ``config.resolve_for_frame(width, height)`` first.
    config:
        A resolved :class:`RegionConfig`.
    """
    per_region: dict[str, list[int]] = {reg.id: [] for reg in config.regions}
    unassigned: list[int] = []
    for idx, box in enumerate(boxes):
        cx, cy = box_center(box)
        assigned = False
        for reg in config.regions:
            if reg.contains_center(cx, cy):
                per_region[reg.id].append(idx)
                assigned = True
                break  # a box belongs to at most one region
        if not assigned:
            unassigned.append(idx)
    if unassigned:
        logger.debug("%d detection(s) outside all regions (ignored)", len(unassigned))
    counts = {rid: len(lst) for rid, lst in per_region.items()}
    return AssignmentResult(per_region=per_region, unassigned=unassigned, counts=counts)
