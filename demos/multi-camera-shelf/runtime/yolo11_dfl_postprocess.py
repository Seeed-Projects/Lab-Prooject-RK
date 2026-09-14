#!/usr/bin/env python3
"""YOLO11 RKNN post-processing: DFL decode + confidence filter + NMS.

Matches the output layout produced by the airockchip/ultralytics_yolo11 export
(the RKNN-friendly YOLO11 head), as used by airockchip/rknn_model_zoo. The
exported model concatenates the 3 detection scales into two tensors:

    output[0]: box/reg branch  shape [1, 4*reg_max, num_anchors]
    output[1]: cls   branch    shape [1, num_classes, num_anchors]

For imgsz=640 and reg_max=16, num_anchors = 80*80 + 40*40 + 20*20 = 8400.

IMPORTANT: always print the real RKNN output tensor count + shapes first
(see infer_video_rknn.py) and confirm they match this layout. If your export
yields per-scale tensors instead, adapt the decode loop accordingly. Do NOT
blindly copy a COCO postprocess.

Only class 0 (``product``) is kept.
"""

from __future__ import annotations

from typing import Sequence

import cv2
import numpy as np

# YOLO11 anchors for imgsz=640: 3 scales, anchor-free, grid centers.
STRIDES = (8, 16, 32)
REG_MAX = 16


def make_grid(imgsz: int = 640, strides: Sequence[int] = STRIDES) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Precompute anchor centers (cx, cy) in input pixels and per-anchor stride.

    Returns (cx [N], cy [N], stride [N]) ordered scale 8 -> 16 -> 32.
    """
    cxs, cys, sts = [], [], []
    for s in strides:
        n = imgsz // s
        for y in range(n):
            for x in range(n):
                cxs.append((x + 0.5) * s)
                cys.append((y + 0.5) * s)
                sts.append(s)
    return (np.array(cxs, dtype=np.float32),
            np.array(cys, dtype=np.float32),
            np.array(sts, dtype=np.float32))


def _softmax(x: np.ndarray, axis: int = -1) -> np.ndarray:
    e = np.exp(x - np.max(x, axis=axis, keepdims=True))
    return e / np.sum(e, axis=axis, keepdims=True)


def dfl_decode(reg: np.ndarray) -> np.ndarray:
    """Decode DFL box branch to 4 distances (left, top, right, bottom) per anchor.

    reg: [4*reg_max, N] -> [N, 4]
    """
    reg = reg.reshape(4, REG_MAX, -1)  # [4, reg_max, N]
    prob = _softmax(reg, axis=1)  # softmax over reg_max
    proj = np.arange(REG_MAX, dtype=np.float32).reshape(1, -1, 1)
    dist = np.sum(prob * proj, axis=1)  # [4, N]
    return dist.T  # [N, 4]


def postprocess(
    outputs: Sequence[np.ndarray],
    img_h: int,
    img_w: int,
    conf: float = 0.30,
    iou: float = 0.45,
    keep_class: int = 0,
    imgsz: int = 640,
) -> tuple[list[list[float]], list[float]]:
    """Decode RKNN YOLO11 outputs to boxes + confidences in original image coords.

    Parameters
    ----------
    outputs:
        RKNN output tensors. Expected: [reg (1,4*reg_max,N), cls (1,nc,N)].
    img_h, img_w:
        Original image size to map boxes back (after letterbox inversion).
    conf, iou:
        Confidence and NMS IoU thresholds.
    keep_class:
        Class id to keep (0 = product).
    imgsz:
        Model input size used for the anchor grid.
    """
    if len(outputs) < 2:
        raise RuntimeError(f"expected >=2 RKNN outputs, got {len(outputs)}")

    reg = outputs[0]  # [1, 4*reg_max, N] or [4*reg_max, N]
    cls = outputs[1]  # [1, nc, N] or [nc, N]
    reg = reg.reshape(reg.shape[-2], reg.shape[-1]) if reg.ndim >= 2 else reg
    cls = cls.reshape(cls.shape[-2], cls.shape[-1]) if cls.ndim >= 2 else cls

    n = reg.shape[-1]
    cx, cy, sts = make_grid(imgsz)
    if cx.shape[0] != n:
        raise RuntimeError(f"anchor grid ({cx.shape[0]}) != output anchors ({n}); check imgsz/export")

    dist = dfl_decode(reg)  # [N, 4] = (left, top, right, bottom) in stride units
    x1 = cx - dist[:, 0] * sts
    y1 = cy - dist[:, 1] * sts
    x2 = cx + dist[:, 2] * sts
    y2 = cy + dist[:, 3] * sts

    cls_sig = 1.0 / (1.0 + np.exp(-cls))  # [nc, N]
    scores = cls_sig[keep_class] if keep_class < cls_sig.shape[0] else cls_sig[0]
    mask = scores >= conf
    x1, y1, x2, y2, sc = x1[mask], y1[mask], x2[mask], y2[mask], scores[mask]
    if sc.size == 0:
        return [], []

    # NMS (cv2 expects [x, y, w, h] + scores)
    boxes_wh = np.stack([x1, y1, x2 - x1, y2 - y1], axis=1)
    idxs = cv2.dnn.NMSBoxes(boxes_wh.tolist(), sc.tolist(), conf, iou)
    keep = np.array(idxs).reshape(-1) if len(idxs) > 0 else np.array([], dtype=int)

    # letterbox inversion: boxes are in imgsz coords; scale back to original
    scale = min(imgsz / img_w, imgsz / img_h)
    pad_w = (imgsz - img_w * scale) / 2
    pad_h = (imgsz - img_h * scale) / 2

    boxes: list[list[float]] = []
    confs: list[float] = []
    for k in keep:
        bx1 = (x1[k] - pad_w) / scale
        by1 = (y1[k] - pad_h) / scale
        bx2 = (x2[k] - pad_w) / scale
        by2 = (y2[k] - pad_h) / scale
        bx1 = max(0.0, min(bx1, img_w))
        by1 = max(0.0, min(by1, img_h))
        bx2 = max(0.0, min(bx2, img_w))
        by2 = max(0.0, min(by2, img_h))
        boxes.append([float(bx1), float(by1), float(bx2), float(by2)])
        confs.append(float(sc[k]))
    return boxes, confs


def letterbox(img: np.ndarray, imgsz: int = 640) -> tuple[np.ndarray, float, float, float]:
    """Resize+pad image to imgsz (letterbox).

    Returns (blob[1,3,imgsz,imgsz], scale, pad_w, pad_h).
    """
    h, w = img.shape[:2]
    scale = min(imgsz / w, imgsz / h)
    nw, nh = int(w * scale), int(h * scale)
    im = cv2.resize(img, (nw, nh))
    pad_w = (imgsz - nw) // 2
    pad_h = (imgsz - nh) // 2
    im = cv2.copyMakeBorder(im, pad_h, imgsz - nh - pad_h, pad_w, imgsz - nw - pad_w,
                            cv2.BORDER_CONSTANT, value=(114, 114, 114))
    blob = im.transpose(2, 0, 1)[None].astype(np.float32) / 255.0
    return blob, scale, float(pad_w), float(pad_h)
