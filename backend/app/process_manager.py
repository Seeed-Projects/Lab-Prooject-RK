from __future__ import annotations

import asyncio
import json
import os
import signal
import uuid
from pathlib import Path
from typing import Any

from .adapter_factory import create_adapter
from .adapters.base import BaseAdapter
from .domain import ACTIVE_STATES, ConflictError, NotFoundError, RunRecord, utc_now
from .events import EventBus
from .registry import DemoRegistry
from .resources import ResourceManager


class ProcessManager:
    def __init__(self, registry: DemoRegistry, data_dir: Path, bus: EventBus, resources: ResourceManager):
        self.registry = registry
        self.data_dir = data_dir.resolve()
        self.bus = bus
        self.resources = resources
        self.runs: dict[str, RunRecord] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._adapters: dict[str, BaseAdapter] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._adapter_tasks: dict[str, list[asyncio.Task[None]]] = {}
        self._lock = asyncio.Lock()
        self.data_dir.mkdir(parents=True, exist_ok=True)

    async def start(self, demo_id: str, parameters: dict[str, Any] | None = None) -> RunRecord:
        manifest = self.registry.get(demo_id)
        adapter = create_adapter(manifest)
        errors = adapter.validate()
        if errors:
            raise ConflictError("; ".join(errors))
        async with self._lock:
            if manifest.lifecycle.get("single_instance", True):
                active = [r for r in self.runs.values() if r.demo_id == demo_id and r.state in ACTIVE_STATES]
                if active:
                    raise ConflictError(f"demo already active: {active[0].run_id}")
            run_id = f"run_{uuid.uuid4().hex}"
            run_dir = self.data_dir / "runs" / run_id
            (run_dir / "artifacts").mkdir(parents=True, exist_ok=False)
            record = RunRecord(run_id=run_id, demo_id=demo_id, run_dir=run_dir)
            self.runs[run_id] = record
            self._adapters[run_id] = adapter
        try:
            await self.resources.acquire(manifest, run_id)
            command = adapter.start(run_dir, parameters or {})
            process = await asyncio.create_subprocess_exec(
                *command.argv,
                cwd=str(command.cwd),
                env=command.env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except Exception as exc:
            record.state = "failed"
            record.health = "unhealthy"
            record.last_error = str(exc)
            record.finished_at = utc_now()
            await self.resources.release(run_id)
            self._persist(record)
            raise
        record.pid = process.pid
        record.started_at = utc_now()
        record.ready = adapter.ready_on_start()
        record.state = "running" if record.ready else "starting"
        record.health = "healthy" if record.ready else "starting"
        self._processes[run_id] = process
        self._persist(record)
        await self._status_event(record)
        self._adapter_tasks[run_id] = [
            asyncio.create_task(task) for task in adapter.background_tasks(
                run_dir,
                lambda channel, event_type, payload: self._event(record, channel, event_type, payload),
            )
        ]
        self._tasks[run_id] = asyncio.create_task(self._supervise(record, process, adapter))
        return record

    async def stop(self, run_id: str) -> RunRecord:
        record = self.get(run_id)
        if record.state not in ACTIVE_STATES:
            return record
        process = self._processes.get(run_id)
        if not process or process.returncode is not None:
            return record
        record.state = "stopping"
        record.health = "stopping"
        self._persist(record)
        await self._status_event(record)
        adapter = self._adapters[run_id]
        try:
            os.killpg(process.pid, adapter.stop_signal())
        except ProcessLookupError:
            return record
        try:
            await asyncio.wait_for(process.wait(), timeout=adapter.stop_timeout())
        except asyncio.TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
        return record

    def get(self, run_id: str) -> RunRecord:
        try:
            return self.runs[run_id]
        except KeyError as exc:
            raise NotFoundError(f"run not found: {run_id}") from exc

    def list_for_demo(self, demo_id: str) -> list[RunRecord]:
        self.registry.get(demo_id)
        return sorted((r for r in self.runs.values() if r.demo_id == demo_id), key=lambda r: r.created_at, reverse=True)

    def logs(self, run_id: str, stream: str = "all", offset: int = 0, limit: int = 200) -> dict[str, Any]:
        record = self.get(run_id)
        names = ["stdout", "stderr"] if stream == "all" else [stream]
        result: dict[str, list[str]] = {}
        for name in names:
            if name not in {"stdout", "stderr"}:
                continue
            path = record.run_dir / f"{name}.log"
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.exists() else []
            result[name] = lines[offset: offset + min(limit, 1000)]
        return {"run_id": run_id, "offset": offset, "logs": result}

    def artifacts(self, run_id: str) -> list[dict[str, Any]]:
        record = self.get(run_id)
        return self._adapters[run_id].discover_artifacts(record.run_dir)

    def public_run(self, record: RunRecord) -> dict[str, Any]:
        data = record.public_dict()
        adapter = self._adapters.get(record.run_id)
        if adapter:
            data["adapter_status"] = adapter.status(record.run_dir)
        return data

    def artifact(self, run_id: str, name: str) -> tuple[Path, str]:
        record = self.get(run_id)
        manifest = self.registry.get(record.demo_id)
        for item in manifest.outputs.get("artifacts", []):
            if item.get("name") == name:
                path = self._adapters[run_id].artifact_path(record.run_dir, item)
                if not path.is_file():
                    raise NotFoundError(f"artifact not found: {name}")
                return path, item.get("media_type", "application/octet-stream")
        raise NotFoundError(f"artifact not found: {name}")

    async def shutdown(self) -> None:
        for record in list(self.runs.values()):
            if record.state in ACTIVE_STATES:
                await self.stop(record.run_id)
        tasks = list(self._tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _supervise(self, record: RunRecord, process: asyncio.subprocess.Process, adapter: BaseAdapter) -> None:
        stdout_task = asyncio.create_task(self._pump(record, process.stdout, "stdout", adapter))
        stderr_task = asyncio.create_task(self._pump(record, process.stderr, "stderr", adapter))
        exit_code = await process.wait()
        await asyncio.gather(stdout_task, stderr_task)
        was_stopping = record.state == "stopping"
        record.exit_code = exit_code
        record.finished_at = utc_now()
        if was_stopping:
            record.state, record.health = "stopped", "stopped"
        elif exit_code == 0:
            record.state, record.health = "completed", "healthy"
        else:
            record.state, record.health = "failed", "unhealthy"
            record.last_error = record.last_error or f"process exited with code {exit_code}"
        record.metrics["artifacts"] = len(adapter.discover_artifacts(record.run_dir))
        adapter_tasks = self._adapter_tasks.pop(record.run_id, [])
        for task in adapter_tasks:
            task.cancel()
        if adapter_tasks:
            await asyncio.gather(*adapter_tasks, return_exceptions=True)
        self._persist(record)
        await self.resources.release(record.run_id)
        await self._status_event(record)
        for item in adapter.discover_artifacts(record.run_dir):
            await self._event(record, "artifact", "artifact.ready", item)
        self._processes.pop(record.run_id, None)

    async def _pump(
        self,
        record: RunRecord,
        reader: asyncio.StreamReader | None,
        stream: str,
        adapter: BaseAdapter,
    ) -> None:
        if reader is None:
            return
        log_path = record.run_dir / f"{stream}.log"
        with log_path.open("a", encoding="utf-8") as log:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                log.write(line + "\n")
                log.flush()
                await self._event(record, "log", f"log.{stream}", {"line": line})
                if not record.ready and adapter.is_ready_line(line, stream):
                    record.ready, record.state, record.health = True, "running", "healthy"
                    self._persist(record)
                    await self._status_event(record)
                for channel, event_type, payload in adapter.parse_line(line, stream):
                    if event_type == "metrics.fps" and "fps" in payload:
                        record.metrics["fps"] = payload["fps"]
                    await self._event(record, channel, event_type, payload)

    async def _status_event(self, record: RunRecord) -> None:
        await self._event(record, "status", f"hub.run.{record.state}", self.public_run(record))

    async def _event(self, record: RunRecord, channel: str, event_type: str, payload: dict[str, Any]) -> None:
        await self.bus.publish(
            record.demo_id, record.run_id, channel, event_type, payload,
            persist_path=record.run_dir / "events.jsonl",
        )

    def _persist(self, record: RunRecord) -> None:
        path = record.run_dir / "status.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record.public_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
