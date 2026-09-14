"""Fixed shelf type mapping plus short-lived held-product type tracking."""

from __future__ import annotations

from shelf_monitor.regions import RegionConfig, assign_detections_to_regions, box_center


def type_labels(
    boxes: list[list[float]],
    config: RegionConfig | None,
    type_groups: dict[str, int],
    type_names: dict[str, str],
) -> list[str]:
    labels = [""] * len(boxes)
    if config is None or not boxes:
        return labels
    assignment = assign_detections_to_regions(boxes, config)
    for fallback_id, region in enumerate(config.regions, start=1):
        numeric = str(type_groups.get(region.id, fallback_id))
        display = type_names.get(numeric, numeric)
        for detection_idx in assignment.per_region[region.id]:
            labels[detection_idx] = display
    return labels


class HeldTypeTracker:
    def __init__(
        self,
        config: RegionConfig | None,
        type_groups: dict[str, int],
        type_names: dict[str, str],
    ) -> None:
        self.config = config
        self.type_groups = type_groups
        self.type_names = type_names
        self.tracks: list[dict] = []

    def _nearest_label(self, cx: float, cy: float) -> str:
        if self.config is None:
            return ""
        fallback_id, region = min(
            enumerate(self.config.regions, start=1),
            key=lambda item: (cx - (item[1].pixel_roi[0] + item[1].pixel_roi[2]) / 2) ** 2
                           + (cy - (item[1].pixel_roi[1] + item[1].pixel_roi[3]) / 2) ** 2,
        )
        numeric = str(self.type_groups.get(region.id, fallback_id))
        return self.type_names.get(numeric, numeric)

    def update(self, boxes: list[list[float]], timestamp: float) -> list[str]:
        if self.config is None:
            return [""] * len(boxes)
        self.tracks = [track for track in self.tracks if timestamp - track["time"] <= 1.0]
        initial = type_labels(boxes, self.config, self.type_groups, self.type_names)
        labels: list[str] = []
        used: set[int] = set()
        for box, detected_label in zip(boxes, initial):
            cx, cy = box_center(tuple(box))
            candidates = [
                (idx, (cx - track["cx"]) ** 2 + (cy - track["cy"]) ** 2)
                for idx, track in enumerate(self.tracks)
                if idx not in used
            ]
            match = min(candidates, key=lambda item: item[1]) if candidates else None
            if match is not None and match[1] <= 180 ** 2:
                track_idx = match[0]
                track = self.tracks[track_idx]
                label = track["label"] or detected_label or self._nearest_label(cx, cy)
                track.update(cx=cx, cy=cy, time=timestamp, label=label)
                used.add(track_idx)
            else:
                label = detected_label or self._nearest_label(cx, cy)
                self.tracks.append({"cx": cx, "cy": cy, "time": timestamp, "label": label})
                used.add(len(self.tracks) - 1)
            labels.append(label)
        return labels

