from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .base import BaseAdapter, CommandSpec


FPS_RE = re.compile(r"(?:fps|FPS)\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)")
PROGRESS_RE = re.compile(r"(?:frame|Frame)\s*[:=]?\s*(\d+)")


class RetailShelfAdapter(BaseAdapter):
    def build_command(self, run_dir: Path, parameters: dict[str, Any]) -> CommandSpec:
        self.validate_parameters(parameters)
        return CommandSpec(self.manifest.entry, self.manifest.project_root, dict(__import__("os").environ))

    def parse_line(self, line: str, stream: str) -> list[tuple[str, str, dict[str, Any]]]:
        events: list[tuple[str, str, dict[str, Any]]] = []
        fps = FPS_RE.search(line)
        if fps:
            events.append(("metrics", "metrics.fps", {"fps": float(fps.group(1))}))
        progress = PROGRESS_RE.search(line)
        if progress:
            events.append(("data", "video.progress", {"frame": int(progress.group(1))}))
        return events

