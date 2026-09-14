from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .base import BaseAdapter, CommandSpec


FPS_RE = re.compile(r"(?:fps|FPS)\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)")
FRAME_RE = re.compile(r"(?:frame|Frame)\s*[:=]?\s*(\d+)")
TENSOR_RE = re.compile(r"(?:shape|Shape).*?(\[[^\]]+\]|\([^)]+\))")


class ShelfAdapter(BaseAdapter):
    def build_command(self, run_dir: Path, parameters: dict[str, Any]) -> CommandSpec:
        self.validate_parameters(parameters)
        env = dict(os.environ)
        env.setdefault("PYTHONUNBUFFERED", "1")
        return CommandSpec(self.manifest.entry, self.manifest.project_root, env)

    def parse_line(self, line: str, stream: str) -> list[tuple[str, str, dict[str, Any]]]:
        events: list[tuple[str, str, dict[str, Any]]] = []
        fps = FPS_RE.search(line)
        if fps:
            events.append(("metrics", "metrics.fps", {"fps": float(fps.group(1))}))
        frame = FRAME_RE.search(line)
        if frame:
            events.append(("data", "video.progress", {"frame": int(frame.group(1))}))
        tensor = TENSOR_RE.search(line)
        if tensor:
            events.append(("data", "vision.tensor_shape", {"shape": tensor.group(1), "line": line}))
        if "init_runtime" in line:
            events.append(("status", "vision.rknn_runtime", {"line": line}))
        return events

    def status(self, run_dir: Path) -> dict[str, Any]:
        video = self.manifest.project_root / "outputs" / "restock_demo_rk3588.mp4"
        return {
            "output_video_exists": video.is_file(),
            "output_video": str(video),
            "output_video_size": video.stat().st_size if video.is_file() else 0,
        }
