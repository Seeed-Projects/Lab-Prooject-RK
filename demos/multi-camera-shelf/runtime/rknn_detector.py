"""Small RKNNLite detector wrapper used by the RK3588 video application."""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from .yolo_postprocess import letterbox_rgb, postprocess_auto

logger = logging.getLogger("runtime.rknn_detector")


class RKNNDetector:
    def __init__(
        self,
        model_path: str | Path,
        imgsz: int,
        conf: float,
        iou: float,
        core_mask: str = "auto",
    ) -> None:
        self.model_path = Path(model_path)
        self.imgsz = int(imgsz)
        self.conf = float(conf)
        self.iou = float(iou)
        self._printed_shapes = False
        if not self.model_path.exists():
            raise FileNotFoundError(f"RKNN model not found: {self.model_path}")
        try:
            from rknnlite.api import RKNNLite
        except ImportError as exc:
            raise RuntimeError(
                "rknn-toolkit-lite2 is not installed. Install the Rockchip wheel "
                "matching this board's Python and librknnrt versions."
            ) from exc
        self._api = RKNNLite
        self.rknn = RKNNLite()
        ret = self.rknn.load_rknn(str(self.model_path))
        if ret != 0:
            raise RuntimeError(f"load_rknn failed ({ret}): {self.model_path}")
        kwargs = {}
        if core_mask and core_mask.lower() != "auto":
            constant = getattr(RKNNLite, core_mask, None)
            if constant is None:
                raise ValueError(f"unknown RKNNLite core mask constant: {core_mask}")
            kwargs["core_mask"] = constant
        elif hasattr(RKNNLite, "NPU_CORE_AUTO"):
            kwargs["core_mask"] = RKNNLite.NPU_CORE_AUTO
        ret = self.rknn.init_runtime(**kwargs)
        if ret != 0:
            raise RuntimeError(f"init_runtime failed ({ret}) for {self.model_path}")
        logger.info("loaded RKNN model %s (imgsz=%d)", self.model_path, self.imgsz)

    def predict(self, frame_bgr: np.ndarray) -> tuple[list[list[float]], list[float]]:
        height, width = frame_bgr.shape[:2]
        input_rgb = letterbox_rgb(frame_bgr, self.imgsz)[None, ...]
        try:
            outputs = self.rknn.inference(inputs=[input_rgb], data_format=["nhwc"])
        except TypeError:
            outputs = self.rknn.inference(inputs=[input_rgb])
        if outputs is None:
            raise RuntimeError(f"RKNN inference returned None: {self.model_path}")
        if not self._printed_shapes:
            logger.info(
                "%s output tensors: %s",
                self.model_path.name,
                [tuple(np.asarray(output).shape) for output in outputs],
            )
            self._printed_shapes = True
        return postprocess_auto(outputs, height, width, self.conf, self.iou, self.imgsz)

    def release(self) -> None:
        if getattr(self, "rknn", None) is not None:
            self.rknn.release()
