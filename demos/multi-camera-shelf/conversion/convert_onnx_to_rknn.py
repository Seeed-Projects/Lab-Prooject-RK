#!/usr/bin/env python3
"""Convert one static YOLO11 ONNX model to RKNN for RK3588.

Run this on x86_64 Ubuntu with the Rockchip rknn-toolkit2 wheel that matches
the target board's runtime/driver family. Do not run it on the RK3588 board.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import numpy as np


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--onnx", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--platform", default="rk3588")
    parser.add_argument("--quantize", action="store_true")
    parser.add_argument("--dataset", type=Path, default=None,
                        help="Calibration list required only with --quantize")
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="After building, run one zero-input inference in the Toolkit2 PC simulator",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def check(ret: int, step: str) -> None:
    if ret != 0:
        raise RuntimeError(f"RKNN {step} failed with code {ret}")


def onnx_image_size(path: Path) -> tuple[int, int]:
    """Read the static image height/width from a NCHW or NHWC ONNX input."""
    import onnx

    model = onnx.load(str(path), load_external_data=False)
    dims = [dim.dim_value for dim in model.graph.input[0].type.tensor_type.shape.dim]
    if len(dims) != 4 or not all(dims):
        raise RuntimeError(f"--simulate requires one static 4-D ONNX input, got {dims}")
    if dims[1] in (1, 3, 4):
        return int(dims[2]), int(dims[3])
    if dims[3] in (1, 3, 4):
        return int(dims[1]), int(dims[2])
    raise RuntimeError(f"cannot determine image axes from ONNX input {dims}")


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    if not args.onnx.exists():
        raise FileNotFoundError(args.onnx)
    if args.quantize and (args.dataset is None or not args.dataset.exists()):
        raise FileNotFoundError("--quantize requires a valid --dataset calibration list")
    try:
        from rknn.api import RKNN
    except ImportError as exc:
        raise RuntimeError("Install the matching Rockchip rknn-toolkit2 wheel on x86_64 Ubuntu") from exc

    args.output.parent.mkdir(parents=True, exist_ok=True)
    rknn = RKNN(verbose=args.verbose)
    try:
        check(
            rknn.config(
                target_platform=args.platform,
                mean_values=[[0, 0, 0]],
                std_values=[[255, 255, 255]],
                optimization_level=3,
            ),
            "config",
        )
        check(rknn.load_onnx(model=str(args.onnx)), "load_onnx")
        check(
            rknn.build(
                do_quantization=args.quantize,
                dataset=str(args.dataset) if args.dataset else None,
            ),
            "build",
        )
        check(rknn.export_rknn(str(args.output)), "export_rknn")
        if args.simulate:
            height, width = onnx_image_size(args.onnx)
            check(rknn.init_runtime(), "simulator init_runtime")
            outputs = rknn.inference(
                inputs=[np.zeros((1, height, width, 3), dtype=np.uint8)],
                data_format=["nhwc"],
            )
            if outputs is None:
                raise RuntimeError("RKNN simulator inference returned None")
            logging.info(
                "simulator output shapes: %s",
                [tuple(np.asarray(output).shape) for output in outputs],
            )
    finally:
        rknn.release()
    logging.info("RKNN written: %s (%d bytes)", args.output, args.output.stat().st_size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
