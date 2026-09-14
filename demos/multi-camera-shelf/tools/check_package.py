#!/usr/bin/env python3
"""Validate deployment package structure without requiring an RK3588 NPU."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.parse_args()
    cfg = json.loads((ROOT / "configs" / "runtime.json").read_text(encoding="utf-8"))
    required = [
        "app/infer_video_rknn.py",
        "runtime/rknn_detector.py",
        "runtime/yolo_postprocess.py",
        cfg["video"], cfg["regions"], cfg["type_regions"], cfg["type_groups"],
        cfg["type_names"], cfg["scripted_events"],
        cfg["shelf_model"]["path"],
        cfg["held_model"]["path"],
        "README.md",
        "rknn-toolkit-lite2-packages/rknn_toolkit_lite2-2.3.2-cp311-cp311-manylinux_2_17_aarch64.manylinux2014_aarch64.whl",
    ]
    missing = [item for item in required if not (ROOT / item).exists()]
    if missing:
        print("Missing files:")
        for item in missing:
            print(f"  - {item}")
        return 2
    print("Package structure: OK")
    artifacts = (
        cfg["shelf_model"]["path"],
        cfg["held_model"]["path"],
        "input/demo.mp4",
        "rknn-toolkit-lite2-packages/rknn_toolkit_lite2-2.3.2-cp311-cp311-manylinux_2_17_aarch64.manylinux2014_aarch64.whl",
    )
    for item in artifacts:
        path = ROOT / item
        print(f"{item}: {path.stat().st_size} bytes sha256={digest(path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
