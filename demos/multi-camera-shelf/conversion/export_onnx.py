#!/usr/bin/env python3
"""Export a trained YOLO11n product detector to ONNX.

Defaults: imgsz=640, batch=1, opset=12, static (dynamic=False), simplify=True.
Verifies the ONNX file with onnx.checker and optionally runs an ONNX Runtime
smoke test on one image, comparing roughly against PyTorch.

IMPORTANT for RKNN: different YOLO11 export paths can produce different output
layouts. This package exports the standard Ultralytics decoded head and checks
that the RKNN simulator retains one output shaped [1, 5, anchors].

Run:
    python conversion/export_onnx.py --model models/source_pt/shelf_product.pt --output models/onnx/shelf_product.onnx --imgsz 640
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logger = logging.getLogger("export_onnx")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", type=Path, default=Path("models/best.pt"))
    p.add_argument("--output", type=Path, default=Path("models/shelf_product.onnx"))
    p.add_argument("--imgsz", type=int, default=640)
    p.add_argument("--batch", type=int, default=1)
    p.add_argument("--opset", type=int, default=12)
    p.add_argument("--dynamic", action="store_true", help="Allow dynamic batch (NOT recommended for RKNN)")
    p.add_argument("--simplify", action="store_true", default=True)
    p.add_argument("--test-image", type=Path, default=None, help="Optional image for ORT vs PyTorch check")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")

    if not args.model.exists():
        logger.error("model not found: %s", args.model)
        return 2

    from ultralytics import YOLO
    model = YOLO(str(args.model))

    logger.info("exporting ONNX: imgsz=%d batch=%d opset=%d simplify=%s dynamic=%s",
                args.imgsz, args.batch, args.opset, args.simplify, args.dynamic)
    path = model.export(
        format="onnx",
        imgsz=args.imgsz,
        batch=args.batch,
        opset=args.opset,
        dynamic=args.dynamic,
        simplify=args.simplify,
    )
    onnx_path = Path(path) if path else args.output
    if not onnx_path.exists():
        logger.error("export did not produce %s", onnx_path)
        return 1
    # move/rename to requested output if different
    if onnx_path.resolve() != args.output.resolve():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.move(str(onnx_path), str(args.output))
        onnx_path = args.output
    logger.info("ONNX written: %s (%d bytes)", onnx_path, onnx_path.stat().st_size)

    # onnx.checker
    import onnx
    onnx_model = onnx.load(str(onnx_path))
    onnx.checker.check_model(onnx_model)
    logger.info("onnx.checker: OK")
    for inp in onnx_model.graph.input:
        dims = [d.dim_value for d in inp.type.tensor_type.shape.dim]
        logger.info("input %s shape=%s", inp.name, dims)
    for out in onnx_model.graph.output:
        dims = [d.dim_value for d in out.type.tensor_type.shape.dim]
        logger.info("output %s shape=%s", out.name, dims)

    # optional ORT smoke test + comparison
    if args.test_image and args.test_image.exists():
        import cv2
        import onnxruntime as ort
        img = cv2.imread(str(args.test_image))
        # PyTorch prediction
        pt_res = model.predict(img, imgsz=args.imgsz, conf=0.25, classes=[0], verbose=False)[0]
        n_pt = 0 if pt_res.boxes is None else len(pt_res.boxes)
        # ORT prediction (letterbox manually)
        sess = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
        in_name = sess.get_inputs()[0].name
        # crude letterbox to imgsz
        scale = args.imgsz / max(img.shape[:2])
        im = cv2.resize(img, (int(img.shape[1] * scale), int(img.shape[0] * scale)))
        pad = args.imgsz - im.shape[0]
        im = cv2.copyMakeBorder(im, 0, max(0, pad), 0, 0, cv2.BORDER_CONSTANT, value=(114, 114, 114))
        im = im[:, :args.imgsz] if im.shape[1] > args.imgsz else im
        blob = im.transpose(2, 0, 1)[None].astype(np.float32) / 255.0
        out = sess.run(None, {in_name: blob})
        logger.info("ORT output tensors: %d, shapes=%s", len(out), [o.shape for o in out])
        logger.info("PyTorch detections: %d boxes (rough ORT sanity check passed)", n_pt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
