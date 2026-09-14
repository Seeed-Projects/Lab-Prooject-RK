"""YOLO11 preprocessing and output-layout-aware postprocessing for RKNN."""

from __future__ import annotations

import logging
from typing import Sequence

import cv2
import numpy as np

from .yolo11_dfl_postprocess import postprocess as dfl_postprocess

logger = logging.getLogger("runtime.yolo_postprocess")


def letterbox_rgb(frame_bgr: np.ndarray, imgsz: int) -> np.ndarray:
    """Return an uint8 RGB NHWC image accepted by common RKNN exports."""
    height, width = frame_bgr.shape[:2]
    scale = min(imgsz / width, imgsz / height)
    new_w, new_h = max(1, round(width * scale)), max(1, round(height * scale))
    resized = cv2.resize(frame_bgr, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    left = (imgsz - new_w) // 2
    top = (imgsz - new_h) // 2
    padded = cv2.copyMakeBorder(
        resized,
        top,
        imgsz - new_h - top,
        left,
        imgsz - new_w - left,
        cv2.BORDER_CONSTANT,
        value=(114, 114, 114),
    )
    return cv2.cvtColor(padded, cv2.COLOR_BGR2RGB)


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -50.0, 50.0)))


def _nms_and_restore(
    xyxy: np.ndarray,
    scores: np.ndarray,
    img_h: int,
    img_w: int,
    imgsz: int,
    conf: float,
    iou: float,
) -> tuple[list[list[float]], list[float]]:
    if len(scores) == 0:
        return [], []
    xywh = np.column_stack((xyxy[:, 0], xyxy[:, 1], xyxy[:, 2] - xyxy[:, 0], xyxy[:, 3] - xyxy[:, 1]))
    keep = cv2.dnn.NMSBoxes(xywh.tolist(), scores.tolist(), conf, iou)
    indices = [int(i) for i in np.asarray(keep).reshape(-1)] if len(keep) else []
    scale = min(imgsz / img_w, imgsz / img_h)
    pad_w = (imgsz - img_w * scale) / 2.0
    pad_h = (imgsz - img_h * scale) / 2.0
    boxes: list[list[float]] = []
    confs: list[float] = []
    for idx in indices:
        x1 = float(np.clip((xyxy[idx, 0] - pad_w) / scale, 0, img_w))
        y1 = float(np.clip((xyxy[idx, 1] - pad_h) / scale, 0, img_h))
        x2 = float(np.clip((xyxy[idx, 2] - pad_w) / scale, 0, img_w))
        y2 = float(np.clip((xyxy[idx, 3] - pad_h) / scale, 0, img_h))
        if x2 > x1 and y2 > y1:
            boxes.append([x1, y1, x2, y2])
            confs.append(float(scores[idx]))
    return boxes, confs


def postprocess_standard(
    output: np.ndarray,
    img_h: int,
    img_w: int,
    conf: float,
    iou: float,
    imgsz: int,
) -> tuple[list[list[float]], list[float]]:
    """Decode standard Ultralytics output shaped [1, 4+nc, anchors]."""
    pred = np.asarray(output)
    if pred.ndim == 3 and pred.shape[0] == 1:
        pred = pred[0]
    pred = np.squeeze(pred)
    if pred.ndim == 1:
        pred = pred.reshape(pred.shape[0], 1)
    if pred.ndim != 2:
        raise RuntimeError(f"standard YOLO output must be 2-D after squeeze, got {pred.shape}")
    if 5 <= pred.shape[0] <= 256 and (
        pred.shape[1] < 5 or pred.shape[1] > pred.shape[0]
    ):
        pred = pred.T
    if pred.shape[1] < 5:
        raise RuntimeError(f"standard YOLO output needs at least 5 values/anchor, got {pred.shape}")
    xywh = pred[:, :4].astype(np.float32)
    class_scores = pred[:, 4:].astype(np.float32)
    if class_scores.min(initial=0.0) < 0.0 or class_scores.max(initial=0.0) > 1.0:
        class_scores = _sigmoid(class_scores)
    scores = class_scores.max(axis=1)
    mask = scores >= conf
    xywh, scores = xywh[mask], scores[mask]
    xyxy = np.empty_like(xywh)
    xyxy[:, 0] = xywh[:, 0] - xywh[:, 2] / 2
    xyxy[:, 1] = xywh[:, 1] - xywh[:, 3] / 2
    xyxy[:, 2] = xywh[:, 0] + xywh[:, 2] / 2
    xyxy[:, 3] = xywh[:, 1] + xywh[:, 3] / 2
    return _nms_and_restore(xyxy, scores, img_h, img_w, imgsz, conf, iou)


def postprocess_auto(
    outputs: Sequence[np.ndarray],
    img_h: int,
    img_w: int,
    conf: float,
    iou: float,
    imgsz: int,
) -> tuple[list[list[float]], list[float]]:
    """Select standard decoded-head or Rockchip DFL branch postprocessing."""
    if len(outputs) == 1:
        return postprocess_standard(outputs[0], img_h, img_w, conf, iou, imgsz)
    # Locate the 64-channel regression branch and the class branch. The exact
    # output order can differ between Toolkit2/exporter versions.
    reg_idx = None
    for idx, tensor in enumerate(outputs):
        shape = np.asarray(tensor).shape
        if 64 in shape:
            reg_idx = idx
            break
    if reg_idx is not None:
        cls_idx = next((idx for idx in range(len(outputs)) if idx != reg_idx), None)
        if cls_idx is not None:
            return dfl_postprocess(
                [np.asarray(outputs[reg_idx]), np.asarray(outputs[cls_idx])],
                img_h=img_h,
                img_w=img_w,
                conf=conf,
                iou=iou,
                keep_class=0,
                imgsz=imgsz,
            )
    raise RuntimeError(
        "unsupported RKNN output layout: " + ", ".join(str(np.asarray(out).shape) for out in outputs)
    )
