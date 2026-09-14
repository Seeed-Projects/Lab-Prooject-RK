from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from app.infer_video_rknn import validate_config
from runtime.yolo_postprocess import postprocess_standard


ROOT = Path(__file__).resolve().parent.parent


def test_standard_single_box_decode() -> None:
    output = np.array([[[160.0], [160.0], [100.0], [80.0], [0.9]]], dtype=np.float32)
    boxes, scores = postprocess_standard(output, 320, 320, 0.4, 0.45, 320)
    assert len(boxes) == 1
    assert scores[0] > 0.8
    assert np.allclose(boxes[0], [110.0, 120.0, 210.0, 200.0], atol=1.0)


def test_runtime_config_without_converted_models() -> None:
    cfg = json.loads((ROOT / "configs" / "runtime.json").read_text(encoding="utf-8"))
    assert validate_config(cfg, require_models=False) == []


def test_type_groups_are_shared() -> None:
    groups = json.loads((ROOT / "configs" / "shelf_type_groups_freshco.json").read_text(encoding="utf-8"))
    assert groups["rice_dream_vanilla"] == groups["rice_dream_vanilla_2"]
    assert groups["zevia_cola"] == groups["zevia_black_cherry"]

