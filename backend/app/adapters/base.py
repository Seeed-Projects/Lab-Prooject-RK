from __future__ import annotations

import json
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..domain import Manifest, ValidationError


@dataclass(frozen=True)
class CommandSpec:
    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]


class BaseAdapter:
    def __init__(self, manifest: Manifest):
        self.manifest = manifest

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.manifest.project_root.is_dir():
            errors.append(f"project root missing: {self.manifest.project_root}")
        first_relative = self.manifest.entry[0] not in {"bash", "sh", "python", "python3"}
        if first_relative and "/" in self.manifest.entry[0]:
            candidate = self.manifest.project_root / self.manifest.entry[0]
            if not candidate.exists():
                errors.append(f"entry missing: {candidate}")
        return errors

    def validate_parameters(self, supplied: dict[str, Any]) -> dict[str, Any]:
        unknown = set(supplied) - set(self.manifest.parameters)
        if unknown:
            raise ValidationError(f"unknown parameters: {', '.join(sorted(unknown))}")
        values: dict[str, Any] = {}
        for name, rule in self.manifest.parameters.items():
            value = supplied.get(name, rule.get("default"))
            if value is None:
                continue
            expected = rule.get("type")
            if expected == "string" and not isinstance(value, str):
                raise ValidationError(f"parameter {name} must be a string")
            if expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
                raise ValidationError(f"parameter {name} must be an integer")
            if "enum" in rule and value not in rule["enum"]:
                raise ValidationError(f"parameter {name} is not an allowed value")
            if "minimum" in rule and value < rule["minimum"]:
                raise ValidationError(f"parameter {name} is below minimum")
            if "maximum" in rule and value > rule["maximum"]:
                raise ValidationError(f"parameter {name} exceeds maximum")
            values[name] = value
        return values

    def build_command(self, run_dir: Path, parameters: dict[str, Any]) -> CommandSpec:
        self.validate_parameters(parameters)
        return CommandSpec(self.manifest.entry, self.manifest.project_root, dict(os.environ))

    def start(self, run_dir: Path, parameters: dict[str, Any]) -> CommandSpec:
        return self.build_command(run_dir, parameters)

    def ready_on_start(self) -> bool:
        return True

    def is_ready_line(self, line: str, stream: str) -> bool:
        return False

    def parse_line(self, line: str, stream: str) -> list[tuple[str, str, dict[str, Any]]]:
        return []

    def stop_signal(self) -> signal.Signals:
        name = str(self.manifest.lifecycle.get("stop_signal", "SIGTERM"))
        return getattr(signal, name, signal.SIGTERM)

    def stop_timeout(self) -> float:
        return float(self.manifest.lifecycle.get("stop_timeout_sec", 20))

    def status(self, run_dir: Path) -> dict[str, Any]:
        return {}

    def background_tasks(
        self,
        run_dir: Path,
        emit: Callable[[str, str, dict[str, Any]], Awaitable[None]],
    ) -> list[Awaitable[None]]:
        return []

    def discover_artifacts(self, run_dir: Path) -> list[dict[str, Any]]:
        artifacts = []
        for item in self.manifest.outputs.get("artifacts", []):
            path = self.artifact_path(run_dir, item)
            if path.is_file():
                artifacts.append({
                    "name": item["name"],
                    "media_type": item.get("media_type", "application/octet-stream"),
                    "size": path.stat().st_size,
                    "path": str(path),
                })
        return artifacts

    def artifact_path(self, run_dir: Path, item: dict[str, Any]) -> Path:
        base = run_dir if item.get("base") == "run" else self.manifest.project_root
        candidate = (base / item["path"]).resolve()
        if candidate != base.resolve() and base.resolve() not in candidate.parents:
            raise ValidationError("artifact path escapes its allowed root")
        return candidate

    @staticmethod
    def try_json(line: str) -> dict[str, Any] | None:
        try:
            value = json.loads(line)
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            return None
