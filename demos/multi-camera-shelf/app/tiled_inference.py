"""Overlapping tiled inference for the 320x320 held-product model."""

from __future__ import annotations

import cv2
import numpy as np

from runtime.rknn_detector import RKNNDetector


def _origins(length: int, tile: int, stride: int) -> list[int]:
    if length <= tile:
        return [0]
    positions = list(range(0, length - tile + 1, stride))
    if positions[-1] != length - tile:
        positions.append(length - tile)
    return positions


def detect_tiled(
    detector: RKNNDetector,
    frame,
    tile_size: int = 320,
    overlap: int = 80,
    nms_iou: float = 0.35,
) -> tuple[list[list[float]], list[float]]:
    height, width = frame.shape[:2]
    tile = min(tile_size, height, width)
    stride = max(1, tile - overlap)
    boxes: list[list[float]] = []
    scores: list[float] = []
    for oy in _origins(height, tile, stride):
        for ox in _origins(width, tile, stride):
            crop = frame[oy:oy + tile, ox:ox + tile]
            crop_boxes, crop_scores = detector.predict(crop)
            for box, score in zip(crop_boxes, crop_scores):
                boxes.append([box[0] + ox, box[1] + oy, box[2] + ox, box[3] + oy])
                scores.append(score)
    if not boxes:
        return [], []
    xywh = [[box[0], box[1], box[2] - box[0], box[3] - box[1]] for box in boxes]
    keep = cv2.dnn.NMSBoxes(xywh, scores, detector.conf, nms_iou)
    indices = [int(i) for i in np.asarray(keep).reshape(-1)] if len(keep) else []
    return [boxes[i] for i in indices], [scores[i] for i in indices]

