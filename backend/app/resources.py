from __future__ import annotations

import asyncio
from typing import Any

from .domain import ConflictError, Manifest


class ResourceManager:
    def __init__(self):
        self._owners: dict[str, tuple[str, str]] = {}
        self._by_run: dict[str, set[str]] = {}
        self._lock = asyncio.Lock()

    def keys_for(self, manifest: Manifest) -> set[str]:
        resources = manifest.resources
        keys: set[str] = set()
        npu = resources.get("npu")
        if isinstance(npu, dict) and npu.get("mode", "exclusive") == "exclusive":
            keys.add("npu")
        audio = resources.get("audio")
        if isinstance(audio, dict) and audio.get("mode", "exclusive") == "exclusive":
            keys.add(f"audio:{audio.get('alsa_card', 'default')}")
        camera = resources.get("camera")
        if isinstance(camera, dict):
            keys.add(f"camera:{camera.get('device', 'default')}")
        for port in resources.get("ports", []):
            keys.add(f"port:{int(port)}")
        if manifest.port:
            keys.add(f"port:{manifest.port}")
        return keys

    async def acquire(self, manifest: Manifest, run_id: str) -> None:
        keys = self.keys_for(manifest)
        async with self._lock:
            conflicts = {key: self._owners[key] for key in keys if key in self._owners}
            if conflicts:
                detail = ", ".join(f"{k} held by {v[0]}/{v[1]}" for k, v in conflicts.items())
                raise ConflictError(f"resource conflict: {detail}")
            for key in keys:
                self._owners[key] = (manifest.name, run_id)
            self._by_run[run_id] = keys

    async def release(self, run_id: str) -> None:
        async with self._lock:
            for key in self._by_run.pop(run_id, set()):
                if self._owners.get(key, (None, None))[1] == run_id:
                    self._owners.pop(key, None)

    def snapshot(self) -> dict[str, Any]:
        return {key: {"demo_id": owner[0], "run_id": owner[1]} for key, owner in self._owners.items()}

