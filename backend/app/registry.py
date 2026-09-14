from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .domain import Manifest, NotFoundError, ValidationError


NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")


class DemoRegistry:
    def __init__(self, plugin_dir: Path):
        self.plugin_dir = plugin_dir.resolve()
        self._manifests: dict[str, Manifest] = {}
        self.errors: dict[str, str] = {}

    def scan(self) -> None:
        manifests: dict[str, Manifest] = {}
        errors: dict[str, str] = {}
        if not self.plugin_dir.exists():
            self._manifests, self.errors = {}, {}
            return
        for path in sorted(self.plugin_dir.glob("*/manifest.json")):
            try:
                manifest = self._load(path)
                if manifest.name in manifests:
                    raise ValidationError(f"duplicate demo name: {manifest.name}")
                manifests[manifest.name] = manifest
            except (OSError, json.JSONDecodeError, ValidationError) as exc:
                errors[str(path)] = str(exc)
        self._manifests, self.errors = manifests, errors

    def list(self) -> list[Manifest]:
        return list(self._manifests.values())

    def get(self, demo_id: str) -> Manifest:
        try:
            return self._manifests[demo_id]
        except KeyError as exc:
            raise NotFoundError(f"demo not found: {demo_id}") from exc

    def _load(self, path: Path) -> Manifest:
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        required = ("name", "type", "entry", "port", "description", "project_root")
        missing = [key for key in required if key not in raw]
        if missing:
            raise ValidationError(f"missing required fields: {', '.join(missing)}")
        name = raw["name"]
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise ValidationError("name must be lowercase kebab-case")
        entry = raw["entry"]
        if not isinstance(entry, list) or not entry or not all(isinstance(x, str) and x for x in entry):
            raise ValidationError("entry must be a non-empty string array")
        port = raw["port"]
        if port is not None and (not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535):
            raise ValidationError("port must be null or an integer in 1..65535")
        project_root = Path(raw["project_root"]).expanduser()
        if not project_root.is_absolute():
            project_root = path.parent / project_root
        project_root = project_root.resolve()
        if not project_root.is_dir():
            raise ValidationError(f"project_root does not exist: {project_root}")
        if not isinstance(raw["type"], str) or not isinstance(raw["description"], str):
            raise ValidationError("type and description must be strings")
        for key in ("adapter", "lifecycle", "resources", "outputs", "parameters"):
            if key in raw and not isinstance(raw[key], dict):
                raise ValidationError(f"{key} must be an object")
        return Manifest(
            name=name,
            type=raw["type"],
            entry=tuple(entry),
            port=port,
            description=raw["description"],
            project_root=project_root,
            version=str(raw.get("version", "1.0")),
            adapter=raw.get("adapter", {}),
            lifecycle=raw.get("lifecycle", {}),
            resources=raw.get("resources", {}),
            outputs=raw.get("outputs", {}),
            parameters=raw.get("parameters", {}),
            manifest_path=path.resolve(),
        )
