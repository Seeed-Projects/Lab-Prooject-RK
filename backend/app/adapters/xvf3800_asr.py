from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .base import BaseAdapter, CommandSpec


class XVF3800ASRAdapter(BaseAdapter):
    def build_command(self, run_dir: Path, parameters: dict[str, Any]) -> CommandSpec:
        values = self.validate_parameters(parameters)
        language = str(values.get("language", "zh"))
        recluster = int(values.get("recluster_every", 10))
        argv = (
            "bash", "run.sh", "--language", language, "--seconds", "0",
            "--recluster-every", str(recluster), "--out-dir", str(run_dir / "artifacts"),
        )
        return CommandSpec(argv, self.manifest.project_root, dict(os.environ))

    def ready_on_start(self) -> bool:
        return False

    def is_ready_line(self, line: str, stream: str) -> bool:
        return "[preflight] 全部通过" in line or "WebSocket 订阅地址" in line or '"type":"session"' in line

    def parse_line(self, line: str, stream: str) -> list[tuple[str, str, dict[str, Any]]]:
        value = self.try_json(line)
        if not value or "type" not in value:
            return []
        source_type = value.get("type")
        mapping = {"turn": "asr.turn", "session": "asr.session", "relabel": "asr.relabel"}
        event_type = mapping.get(str(source_type))
        return [("data", event_type, value)] if event_type else []

