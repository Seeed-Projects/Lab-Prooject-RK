from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

from .base import BaseAdapter, CommandSpec


class VoiceAdapter(BaseAdapter):
    def build_command(self, run_dir: Path, parameters: dict[str, Any]) -> CommandSpec:
        values = self.validate_parameters(parameters)
        language = str(values.get("language", "zh"))
        recluster = int(values.get("recluster_every", 10))
        ws_port = int(values.get("ws_port", self.manifest.port or 8765))
        argv = (
            "bash",
            "run.sh",
            "--language",
            language,
            "--seconds",
            "0",
            "--recluster-every",
            str(recluster),
            "--ws-port",
            str(ws_port),
            "--out-dir",
            str(run_dir / "artifacts"),
        )
        env = dict(os.environ)
        env.setdefault("PYTHONUNBUFFERED", "1")
        return CommandSpec(argv, self.manifest.project_root, env)

    def ready_on_start(self) -> bool:
        return False

    def is_ready_line(self, line: str, stream: str) -> bool:
        return "[preflight] 全部通过" in line or "WebSocket 订阅地址" in line

    def parse_line(self, line: str, stream: str) -> list[tuple[str, str, dict[str, Any]]]:
        value = self.try_json(line)
        if not value or "type" not in value:
            return []
        return self._event_from_payload(value)

    def status(self, run_dir: Path) -> dict[str, Any]:
        artifacts = run_dir / "artifacts"
        stats = artifacts / "realtime_stats.json"
        transcript = artifacts / "transcript.txt"
        return {
            "websocket": self.manifest.adapter.get("upstream_websocket", f"ws://127.0.0.1:{self.manifest.port or 8765}"),
            "transcript_exists": transcript.is_file(),
            "stats_exists": stats.is_file(),
        }

    def background_tasks(
        self,
        run_dir: Path,
        emit: Callable[[str, str, dict[str, Any]], Awaitable[None]],
    ) -> list[Awaitable[None]]:
        url = str(self.manifest.adapter.get("upstream_websocket", ""))
        return [self._read_upstream_ws(url, emit)] if url else []

    async def _read_upstream_ws(
        self,
        url: str,
        emit: Callable[[str, str, dict[str, Any]], Awaitable[None]],
    ) -> None:
        try:
            import websockets
        except Exception as exc:
            await emit("log", "log.adapter", {"line": f"voice websocket reader disabled: {exc}"})
            return

        await asyncio.sleep(1.0)
        while True:
            try:
                async with websockets.connect(url) as websocket:
                    await emit("status", "voice.websocket.connected", {"url": url})
                    async for message in websocket:
                        try:
                            payload = json.loads(message)
                        except json.JSONDecodeError:
                            await emit("log", "log.adapter", {"line": message})
                            continue
                        if isinstance(payload, dict):
                            for channel, event_type, event_payload in self._event_from_payload(payload):
                                await emit(channel, event_type, event_payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await emit("status", "voice.websocket.retry", {"url": url, "error": str(exc)})
                await asyncio.sleep(2.0)

    @staticmethod
    def _event_from_payload(value: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
        mapping = {"turn": "asr.turn", "session": "asr.session", "relabel": "asr.relabel"}
        event_type = mapping.get(str(value.get("type")))
        return [("data", event_type, value)] if event_type else []
