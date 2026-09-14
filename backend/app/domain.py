from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TERMINAL_STATES = {"stopped", "completed", "failed"}
ACTIVE_STATES = {"starting", "running", "stopping"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Manifest:
    name: str
    type: str
    entry: tuple[str, ...]
    port: int | None
    description: str
    project_root: Path
    version: str = "1.0"
    adapter: dict[str, Any] = field(default_factory=dict)
    lifecycle: dict[str, Any] = field(default_factory=dict)
    resources: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    parameters: dict[str, Any] = field(default_factory=dict)
    manifest_path: Path | None = None

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["entry"] = list(self.entry)
        data["project_root"] = str(self.project_root)
        data["manifest_path"] = str(self.manifest_path) if self.manifest_path else None
        return data


@dataclass
class RunRecord:
    run_id: str
    demo_id: str
    run_dir: Path
    state: str = "starting"
    pid: int | None = None
    created_at: str = field(default_factory=utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    ready: bool = False
    health: str = "unknown"
    exit_code: int | None = None
    last_error: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["run_dir"] = str(self.run_dir)
        return data


class HubError(Exception):
    status_code = 500


class NotFoundError(HubError):
    status_code = 404


class ConflictError(HubError):
    status_code = 409


class ValidationError(HubError):
    status_code = 422

