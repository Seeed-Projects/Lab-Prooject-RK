from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import shutil
import subprocess
import time
import uuid
from urllib import request as urllib_request
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger("uvicorn.error")


class SalesVoiceAdapter:
    def __init__(
        self,
        project_root: Path | None = None,
        upstream_ws: str = "ws://127.0.0.1:8765",
        out_dir: Path | None = None,
    ):
        backend_root = Path(__file__).resolve().parents[1]
        package_root = backend_root.parent
        self.project_root = project_root or package_root / "services" / "voice-pipeline"
        self.upstream_ws = upstream_ws
        self.out_dir = out_dir or backend_root / "var" / "sales_voice"
        self.process: asyncio.subprocess.Process | None = None
        self.last_error: str | None = None
        self.started_at: float | None = None
        self._audio_cache: tuple[float, str] | None = None
        self._ai_ready_at: float | None = None
        self._last_turn: dict[str, Any] | None = None
        self._display_active = False
        self._monitor_task: asyncio.Task[None] | None = None
        self._event_subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._last_broadcast_recovery = 0.0
        self._start_lock = asyncio.Lock()
        self._audio_details = "Audio device has not been probed"
        self._device_ready_value = False
        self._service_ready_value = False
        self._service_error: str | None = "WebSocket has not been probed"
        self._capture_heartbeat = self.out_dir / "capture_heartbeat"
        self._downmix_heartbeat = self.out_dir / "downmix_heartbeat"
        self._audio_level = self.out_dir / "audio_level.json"
        self._pipeline_diagnostics = self.out_dir / "pipeline_diagnostics.json"
        self._capture_fault_log = self.out_dir / "capture_faults.jsonl"
        self._heartbeat_totals: dict[str, int] = {}
        self._last_capture_diagnostics: dict[str, Any] = {}
        self._diagnostics_cache_at = 0.0
        self._last_run_sh_exit: dict[str, Any] | None = None
        self._observed_stage_pids: dict[str, int] = {}
        self._container_pipeline_observed = False
        self._model_health: dict[str, Any] = {
            "stt_ready": False,
            "stt_backend": None,
            "stt_error": "STT health has not been checked",
            "llm_ready": False,
            "llm_model_initialized": False,
            "llm_error": "LLM health has not been checked",
            "checked_at": None,
        }
        self._model_health_checked_at = 0.0
        self._usb_recovery_request = Path(
            os.getenv(
                "XVF3800_USB_RECOVERY_REQUEST",
                str(backend_root / "run" / "xvf3800-usb-recover.request"),
            )
        )
        self._usb_recovery_result = Path(
            os.getenv(
                "XVF3800_USB_RECOVERY_RESULT",
                str(backend_root / "run" / "xvf3800-usb-recover.result.json"),
            )
        )

    async def start(self, language: str = "en") -> dict[str, Any]:
        if language not in {"zh", "en"}:
            raise ValueError("language must be zh or en")
        async with self._start_lock:
            if self.process and self.process.returncode is None:
                self._ensure_monitor()
                return self.status()
            if not self.project_root.is_dir():
                raise FileNotFoundError(f"ASR project root missing: {self.project_root}")
            run_sh = self.project_root / "run.sh"
            if not run_sh.is_file():
                raise FileNotFoundError(f"ASR run.sh missing: {run_sh}")

            self.out_dir.mkdir(parents=True, exist_ok=True)
            # Never accept a heartbeat from a previous process as proof of a
            # new capture session.
            for stale_path in (
                self._capture_heartbeat,
                self._downmix_heartbeat,
                self._audio_level,
                self._pipeline_diagnostics,
            ):
                try:
                    stale_path.unlink()
                except FileNotFoundError:
                    pass
            stage_dir = self.out_dir / "pipeline_stages"
            if stage_dir.is_dir():
                for stage_path in stage_dir.glob("*.json"):
                    try:
                        stage_path.unlink()
                    except FileNotFoundError:
                        pass
            self._heartbeat_totals.clear()
            self._observed_stage_pids.clear()
            self._container_pipeline_observed = False
            self._last_capture_diagnostics = {}
            self._diagnostics_cache_at = 0.0
            self._last_run_sh_exit = None
            argv = (
                "bash",
                "run.sh",
                "--skip-preflight",
                "--language",
                language,
                "--seconds",
                "0",
                "--ws-port",
                "8765",
                "--out-dir",
                str(self.out_dir),
            )
            env = dict(os.environ)
            env.setdefault("PYTHONUNBUFFERED", "1")
            env["NO_PROXY"] = "127.0.0.1,localhost"
            env["no_proxy"] = "127.0.0.1,localhost"
            self.process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(self.project_root),
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            self.last_error = None
            self.started_at = asyncio.get_running_loop().time()
            self._ai_ready_at = None
            self._last_turn = None
            self._ensure_monitor()
            return self.status()

    async def stop(self) -> dict[str, Any]:
        self._display_active = False
        return self.status()

    async def stop_runtime(self) -> dict[str, Any]:
        if not self.process or self.process.returncode is not None:
            if self.process:
                self._remember_run_sh_exit(self.process.pid, self.process.returncode)
            await self._stop_owned_container_pipeline()
            self.process = None
            self._cancel_monitor()
            return self.status()
        try:
            os.killpg(self.process.pid, signal.SIGINT)
        except ProcessLookupError:
            self._remember_run_sh_exit(self.process.pid, self.process.returncode)
            await self._stop_owned_container_pipeline()
            self.process = None
            self._cancel_monitor()
            return self.status()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=30)
        except asyncio.TimeoutError:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await self.process.wait()
        self._remember_run_sh_exit(self.process.pid, self.process.returncode)
        await self._stop_owned_container_pipeline()
        self._cancel_monitor()
        self.process = None
        self._service_ready_value = False
        return self.status()

    async def _signal_owned_container_pipeline(self, signal_number: int) -> bool:
        script = r'''
import os
import signal
import sys

target = sys.argv[1]
signal_number = int(sys.argv[2])
matched = False
for entry in os.listdir("/proc"):
    if not entry.isdigit():
        continue
    try:
        command = open(f"/proc/{entry}/cmdline", "rb").read().replace(b"\\0", b" ").decode()
    except (OSError, UnicodeDecodeError):
        continue
    if "/tmp/realtime_pipeline.py" not in command or target not in command:
        continue
    matched = True
    try:
        os.kill(int(entry), signal_number)
    except (ProcessLookupError, PermissionError):
        pass
print("1" if matched else "0")
'''
        try:
            process = await asyncio.create_subprocess_exec(
                "docker",
                "exec",
                self._container_name(),
                "/opt/venv/bin/python",
                "-c",
                script,
                str(self.out_dir),
                str(signal_number),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=3.0)
            return process.returncode == 0 and stdout.strip() == b"1"
        except (OSError, asyncio.TimeoutError):
            return False

    async def _container_pipeline_exists(self) -> bool:
        return await self._signal_owned_container_pipeline(0)

    async def _stop_owned_container_pipeline(self) -> None:
        if not await self._container_pipeline_exists():
            return
        await self._signal_owned_container_pipeline(signal.SIGINT)
        for _ in range(20):
            await asyncio.sleep(0.25)
            if not await self._container_pipeline_exists():
                return
        await self._signal_owned_container_pipeline(signal.SIGTERM)
        for _ in range(8):
            await asyncio.sleep(0.25)
            if not await self._container_pipeline_exists():
                return
        await self._signal_owned_container_pipeline(signal.SIGKILL)

    @staticmethod
    def _container_name() -> str:
        return os.getenv("VOICE_CONTAINER", "openvoicestream")

    def status(self) -> dict[str, Any]:
        running = bool(self.process and self.process.returncode is None)
        audio_devices = self._audio_details
        device_ready = self._device_ready_value
        service_ready = self._service_ready_value
        service_error = self._service_error
        if running and service_ready:
            runtime_status = "READY"
        elif running:
            runtime_status = "BOOTING"
        else:
            runtime_status = "SUSPENDED"
        display_status = "ACTIVE" if self._display_active else "INACTIVE"
        showcase_ready = running
        return {
            "running": running,
            "showcase_ready": showcase_ready,
            "pid": self.process.pid if running and self.process else None,
            "upstream_ws": self.upstream_ws,
            "project_root": str(self.project_root),
            "out_dir": str(self.out_dir),
            "last_error": self.last_error,
            "audio": audio_devices,
            "device_ready": device_ready,
            "service_ready": service_ready,
            "ai_ready": bool(self._ai_ready_at),
            "service_status": runtime_status,
            "runtime_status": runtime_status,
            "background_runtime": runtime_status,
            "display_active": self._display_active,
            "display_status": display_status,
            "readiness": {
                "device": "DEVICE_READY" if device_ready else "WAITING_FOR_MICROPHONE",
                "service": "SERVICE_READY" if service_ready else "INITIALIZING_ASR",
                "ai": "AI_READY" if self._ai_ready_at else "LISTENING",
            },
            "respeaker_detected": device_ready,
            "service_error": service_error,
            "last_turn": self._last_turn,
            "ai_ready_at": self._ai_ready_at,
            "model_health": self._model_health,
            "capture_health": self.capture_health(),
            "capture_diagnostics": self._last_capture_diagnostics,
        }

    def capture_health(self, *, update_deltas: bool = False) -> dict[str, Any]:
        running = bool(self.process and self.process.returncode is None)
        process_tree = self._owned_process_tree()
        stages = {
            "run_sh": self._stage_snapshot(process_tree, "run.sh", root=True),
            "arecord": self._stage_snapshot(process_tree, "arecord"),
            "host_downmix": self._stage_snapshot(process_tree, "host_downmix.py"),
            "docker_exec": self._stage_snapshot(
                process_tree, ("docker", "exec", "-i", "realtime_pipeline.py")
            ),
        }
        capture_heartbeat = self._heartbeat_snapshot(
            "capture", self._capture_heartbeat, update_baseline=update_deltas
        )
        downmix_heartbeat = self._heartbeat_snapshot(
            "downmix", self._downmix_heartbeat, update_baseline=update_deltas
        )
        container_known = bool(
            self._last_capture_diagnostics.get("container_pipeline_known")
        )
        container_alive = bool(self._last_capture_diagnostics.get("container_pipeline_alive"))
        stages_alive = running and all(stage.get("alive") for stage in stages.values())
        heartbeats_fresh = bool(capture_heartbeat["fresh"] and downmix_heartbeat["fresh"])
        session_started = self.started_at is not None
        stage_diagnostics = self._read_stage_diagnostics() if session_started else {}
        explicit_stage_exit = any(
            isinstance(item.get("returncode"), int)
            for item in stage_diagnostics.values()
        )
        observed_stage_exit = any(
            name != "run_sh"
            and name in self._observed_stage_pids
            and not snapshot.get("alive")
            for name, snapshot in stages.items()
        )
        for name, snapshot in stages.items():
            pid = snapshot.get("pid")
            if name != "run_sh" and snapshot.get("alive") and isinstance(pid, int):
                self._observed_stage_pids[name] = pid
        observed_container_exit = (
            container_known
            and self._container_pipeline_observed
            and not container_alive
        )
        if container_known and container_alive:
            self._container_pipeline_observed = True
        capture_started = bool(
            self._observed_stage_pids
            or stage_diagnostics
            or self._container_pipeline_observed
        )
        startup_pending = running and not capture_started
        process_failure = (
            (session_started and not running)
            or explicit_stage_exit
            or observed_stage_exit
            or observed_container_exit
        )
        failure_kind = None
        if process_failure:
            failure_kind = "CAPTURE_PROCESS_EXITED"
        elif session_started and not heartbeats_fresh and not startup_pending:
            failure_kind = "CAPTURE_HEARTBEAT_STALE"
        return {
            "healthy": stages_alive and (not container_known or container_alive) and heartbeats_fresh,
            "gate_required": True,
            "process_alive": running,
            "capture_process_alive": bool(stages["arecord"].get("alive")),
            "stages_alive": stages_alive,
            "stages": stages,
            "container_pipeline_alive": container_alive,
            "container_pipeline_known": container_known,
            "session_started": session_started,
            "capture_started": capture_started,
            "startup_pending": startup_pending,
            "heartbeat_available": capture_heartbeat["available"],
            "heartbeat_age_seconds": capture_heartbeat["age_seconds"],
            "heartbeat_bytes": capture_heartbeat["file_size"],
            "heartbeat_path": str(self._capture_heartbeat),
            "capture_heartbeat": capture_heartbeat,
            "downmix_heartbeat": downmix_heartbeat,
            "audio_level": self._read_audio_level(),
            "pipeline_stages": stage_diagnostics,
            "failure_kind": failure_kind,
        }

    async def refresh_capture_diagnostics(
        self,
        *,
        force: bool = False,
        include_services: bool = False,
        persist_reason: str | None = None,
    ) -> dict[str, Any]:
        now = time.monotonic()
        if not force and now - self._diagnostics_cache_at < 3.0:
            return self._last_capture_diagnostics

        docker_snapshot = await self._docker_process_snapshot()
        diagnostics: dict[str, Any] = {
            "timestamp": time.time(),
            "timestamp_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "container_running": docker_snapshot["container_running"],
            "container_pipeline_known": (
                docker_snapshot["container_running"]
                and docker_snapshot.get("error") is None
            ),
            "container_pipeline_alive": docker_snapshot["pipeline_alive"],
            "container_pipeline": docker_snapshot["pipeline"],
            "container_pipeline_count": len(docker_snapshot["pipeline"]),
            "container_broadcaster": docker_snapshot["broadcaster"],
            "container_broadcaster_count": len(docker_snapshot["broadcaster"]),
            "docker_error": docker_snapshot.get("error"),
            "run_sh_exit": self._last_run_sh_exit,
            "arecord_stderr_tail": self._read_text_tail(
                self.out_dir / "arecord.stderr.log"
            ),
            "host_downmix_stderr_tail": self._read_text_tail(
                self.out_dir / "host_downmix.stderr.log"
            ),
        }
        if include_services:
            websocket_ready, websocket_error = await self._probe_websocket_readonly()
            control_ready, control_error = await self._probe_tcp_port(8766)
            diagnostics.update(
                {
                    "websocket_8765_ready": websocket_ready,
                    "websocket_8765_error": websocket_error,
                    "control_8766_ready": control_ready,
                    "control_8766_error": control_error,
                }
            )
        self._last_capture_diagnostics = diagnostics
        diagnostics["health"] = self.capture_health(update_deltas=True)
        diagnostics["pipeline_exit"] = self._read_pipeline_diagnostics()
        diagnostics["pipeline_stages"] = self._read_stage_diagnostics()
        diagnostics["run_sh_exit"] = self._last_run_sh_exit
        self._last_capture_diagnostics = diagnostics
        self._diagnostics_cache_at = now
        if persist_reason:
            record = {"reason": persist_reason, **diagnostics}
            self._append_fault_record(record)
            LOGGER.error("[VOICE] capture fault diagnostics=%s", json.dumps(record, ensure_ascii=True))
        return diagnostics

    async def persist_capture_fault_result(self, reason: str) -> dict[str, Any]:
        diagnostics = await self.refresh_capture_diagnostics(
            force=True,
            include_services=True,
        )
        diagnostics["pipeline_exit"] = self._read_pipeline_diagnostics()
        diagnostics["pipeline_stages"] = self._read_stage_diagnostics()
        record = {"reason": f"{reason}_RESULT", **diagnostics}
        self._append_fault_record(record)
        LOGGER.error("[VOICE] capture fault result=%s", json.dumps(record, ensure_ascii=True))
        return diagnostics

    def _heartbeat_snapshot(
        self, name: str, path: Path, *, update_baseline: bool = False
    ) -> dict[str, Any]:
        try:
            stat = path.stat()
            age = max(0.0, time.time() - stat.st_mtime)
            size = stat.st_size
            try:
                total = int(path.read_text(encoding="ascii").strip())
            except (OSError, ValueError):
                total = None
        except FileNotFoundError:
            age = None
            size = 0
            total = None
        previous = self._heartbeat_totals.get(name)
        delta = total - previous if total is not None and previous is not None else None
        if total is not None and update_baseline:
            self._heartbeat_totals[name] = total
        return {
            "available": age is not None,
            "fresh": age is not None and age <= 5.0,
            "age_seconds": round(age, 3) if age is not None else None,
            "last_timestamp": stat.st_mtime if age is not None else None,
            "last_timestamp_iso": (
                time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(stat.st_mtime))
                if age is not None
                else None
            ),
            "bytes_total": total,
            "bytes_delta": delta,
            "file_size": size,
            "path": str(path),
        }

    def _owned_process_tree(self) -> list[dict[str, Any]]:
        if not self.process:
            return []
        pending = [self.process.pid]
        seen: set[int] = set()
        snapshots: list[dict[str, Any]] = []
        while pending:
            pid = pending.pop()
            if pid in seen:
                continue
            seen.add(pid)
            try:
                command = (Path("/proc") / str(pid) / "cmdline").read_bytes()
                stat_fields = (Path("/proc") / str(pid) / "stat").read_text().split()
                children = (
                    Path("/proc") / str(pid) / "task" / str(pid) / "children"
                ).read_text(encoding="ascii")
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
            argv = command.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()
            snapshots.append(
                {
                    "pid": pid,
                    "alive": True,
                    "state": stat_fields[2] if len(stat_fields) > 2 else None,
                    "command": argv,
                    "returncode": self.process.returncode if pid == self.process.pid else None,
                    "exit_signal": (
                        -self.process.returncode
                        if pid == self.process.pid and self.process.returncode is not None and self.process.returncode < 0
                        else None
                    ),
                }
            )
            pending.extend(int(child) for child in children.split() if child.isdigit())
        return snapshots

    def _stage_snapshot(
        self,
        process_tree: list[dict[str, Any]],
        token: str | tuple[str, ...],
        *,
        root: bool = False,
    ) -> dict[str, Any]:
        if root and self.process:
            match = next((item for item in process_tree if item["pid"] == self.process.pid), None)
        else:
            tokens = (token,) if isinstance(token, str) else token
            match = next(
                (
                    item
                    for item in process_tree
                    if all(part in item["command"] for part in tokens)
                ),
                None,
            )
        if match:
            return match
        if root and self.process:
            return {
                "pid": self.process.pid,
                "alive": self.process.returncode is None,
                "state": None,
                "command": "bash run.sh",
                "returncode": self.process.returncode,
                "exit_signal": -self.process.returncode if self.process.returncode is not None and self.process.returncode < 0 else None,
            }
        return {"pid": None, "alive": False, "state": None, "command": None, "returncode": None, "exit_signal": None}

    async def _docker_process_snapshot(self) -> dict[str, Any]:
        try:
            process = await asyncio.create_subprocess_exec(
                "docker", "top", "openvoicestream", "-eo", "pid,ppid,stat,args",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=3.0)
        except (OSError, asyncio.TimeoutError) as exc:
            return {"container_running": False, "pipeline_alive": False, "pipeline": [], "broadcaster": [], "error": str(exc)}
        text = stdout.decode("utf-8", errors="replace")
        lines = [line.strip() for line in text.splitlines()[1:] if line.strip()]
        pipelines = [line for line in lines if "realtime_pipeline.py" in line]
        broadcasters = [line for line in lines if "ws_broadcast.py" in line]
        return {
            "container_running": process.returncode == 0,
            "pipeline_alive": bool(pipelines),
            "pipeline": pipelines,
            "broadcaster": broadcasters,
            "error": stderr.decode("utf-8", errors="replace").strip() or None,
        }

    async def _probe_websocket_readonly(self) -> tuple[bool, str | None]:
        try:
            import websockets
            async with websockets.connect(
                self.upstream_ws, open_timeout=1.5, ping_interval=None, close_timeout=0.5, proxy=None
            ):
                return True, None
        except Exception as exc:
            return False, str(exc)

    @staticmethod
    async def _probe_tcp_port(port: int) -> tuple[bool, str | None]:
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port), timeout=1.0
            )
            writer.close()
            await writer.wait_closed()
            return True, None
        except (OSError, asyncio.TimeoutError) as exc:
            return False, str(exc)

    def _read_pipeline_diagnostics(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self._pipeline_diagnostics.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _read_stage_diagnostics(self) -> dict[str, Any]:
        stage_dir = self.out_dir / "pipeline_stages"
        result: dict[str, Any] = {}
        for stage_path in sorted(stage_dir.glob("*.json")):
            try:
                value = json.loads(stage_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                result[stage_path.stem] = value
        return result

    def _read_audio_level(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self._audio_level.read_text(encoding="ascii"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _read_text_tail(path: Path, limit: int = 4096) -> str | None:
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - limit))
                value = handle.read().decode("utf-8", errors="replace").strip()
        except OSError:
            return None
        return value or None

    def _append_fault_record(self, record: dict[str, Any]) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(record, ensure_ascii=True) + "\n").encode("utf-8")
        descriptor = os.open(
            self._capture_fault_log,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o644,
        )
        try:
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)

    async def probe_model_services(self, *, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if not force and now - self._model_health_checked_at < 5.0:
            return self._model_health

        stt_url = os.getenv("ASR_URL", "http://127.0.0.1:8621").rstrip("/") + "/health"
        llm_url = os.getenv("LLM_SUMMARY_URL", "http://127.0.0.1:8001").rstrip("/") + "/health"
        stt, llm = await asyncio.gather(
            asyncio.to_thread(self._fetch_health_json, stt_url),
            asyncio.to_thread(self._fetch_health_json, llm_url),
        )
        stt_payload, stt_error = stt
        llm_payload, llm_error = llm
        self._model_health = {
            "stt_ready": bool(stt_payload and stt_payload.get("asr") is True),
            "stt_backend": stt_payload.get("asr_backend") if stt_payload else None,
            "stt_error": stt_error,
            "llm_ready": bool(
                llm_payload and llm_payload.get("model_initialized") is True
            ),
            "llm_model_initialized": bool(
                llm_payload and llm_payload.get("model_initialized") is True
            ),
            "llm_error": llm_error,
            "checked_at": time.time(),
        }
        self._model_health_checked_at = now
        return self._model_health

    @staticmethod
    def _fetch_health_json(url: str) -> tuple[dict[str, Any] | None, str | None]:
        try:
            req = urllib_request.Request(url, headers={"Accept": "application/json"})
            with urllib_request.urlopen(req, timeout=3.0) as response:
                value = json.loads(response.read())
            if not isinstance(value, dict):
                return None, "Health response is not a JSON object"
            return value, None
        except Exception as exc:
            return None, str(exc)

    def _remember_run_sh_exit(self, pid: int, returncode: int | None) -> None:
        self._last_run_sh_exit = {
            "pid": pid,
            "returncode": returncode,
            "exit_signal": (
                -returncode if returncode is not None and returncode < 0 else None
            ),
        }

    def hardware_status(self) -> dict[str, Any]:
        return {
            "name": "ReSpeaker",
            "connected": self._device_ready_value,
            "state": "Connected" if self._device_ready_value else "Disconnected",
            "detected_by": "USB audio device",
            "details": self._audio_details,
        }

    async def is_ready(self) -> bool:
        return bool(self.status()["showcase_ready"])

    async def probe_docker(self) -> tuple[bool, str | None]:
        try:
            process = await asyncio.create_subprocess_exec(
                "docker",
                "info",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=3.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return False, "Docker health check timed out"
            if process.returncode == 0:
                return True, None
            return False, stderr.decode("utf-8", errors="replace").strip() or "Docker is not ready"
        except OSError as exc:
            return False, str(exc)

    async def probe_device(self) -> tuple[bool, str | None]:
        now = time.monotonic()
        if self._audio_cache and now - self._audio_cache[0] < 8:
            audio_devices = self._audio_cache[1]
        elif not shutil.which("arecord"):
            audio_devices = "arecord not found"
        else:
            try:
                process = await asyncio.create_subprocess_exec(
                    "arecord",
                    "-l",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                try:
                    stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=1.5)
                    audio_devices = stdout.decode("utf-8", errors="replace")
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                    audio_devices = "arecord device probe timed out"
            except OSError as exc:
                audio_devices = str(exc)
            self._audio_cache = (now, audio_devices)
        ready = self._device_ready(audio_devices)
        self._audio_details = audio_devices
        self._device_ready_value = ready
        return ready, None if ready else "ReSpeaker/XVF3800 capture device is not ready"

    async def recover_usb_device(self, *, timeout: float = 20.0) -> dict[str, Any]:
        """Ask the root-owned helper to reset only the validated XVF3800."""
        request_id = uuid.uuid4().hex
        request = {
            "request_id": request_id,
            "requested_at": time.time(),
            "vendor_id": "2886",
            "product_id": "001a",
        }
        self._usb_recovery_request.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._usb_recovery_result.unlink()
        except FileNotFoundError:
            pass
        temporary = self._usb_recovery_request.with_name(
            f".{self._usb_recovery_request.name}.{request_id}.tmp"
        )
        temporary.write_text(json.dumps(request) + "\n", encoding="ascii")
        os.replace(temporary, self._usb_recovery_request)

        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.25)
            try:
                result = json.loads(
                    self._usb_recovery_result.read_text(encoding="ascii")
                )
            except (FileNotFoundError, OSError, json.JSONDecodeError):
                continue
            if result.get("request_id") != request_id:
                continue
            self._audio_cache = None
            if not result.get("ok"):
                return result

            # USBDEVFS_RESET returns before every userspace view has
            # necessarily converged. Require three fresh ALSA probes.
            consecutive_ready = 0
            settle_deadline = asyncio.get_running_loop().time() + 10.0
            while asyncio.get_running_loop().time() < settle_deadline:
                self._audio_cache = None
                ready, error = await self.probe_device()
                if ready:
                    consecutive_ready += 1
                    if consecutive_ready >= 3:
                        return {**result, "device_ready": True}
                else:
                    consecutive_ready = 0
                    result["device_error"] = error
                await asyncio.sleep(0.5)
            return {
                **result,
                "ok": False,
                "device_ready": False,
                "error": "XVF3800 did not return as a stable ALSA capture device",
            }
        return {
            "request_id": request_id,
            "ok": False,
            "error": "Timed out waiting for the privileged XVF3800 recovery helper",
        }

    async def probe_websocket(self) -> tuple[bool, str | None]:
        try:
            import websockets

            async with websockets.connect(
                self.upstream_ws,
                open_timeout=1.5,
                ping_interval=None,
                close_timeout=0.5,
                proxy=None,
            ):
                self._service_ready_value = True
                self._service_error = None
                return True, None
        except Exception as exc:
            self._service_ready_value = False
            self._service_error = str(exc)
            return False, str(exc)

    def set_display_active(self, active: bool) -> None:
        self._display_active = active
        if active:
            self._ensure_monitor()

    async def subscribe_events(self, max_queue: int = 256) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max_queue)
        self._event_subscribers.add(queue)
        self._ensure_monitor()
        return queue

    async def unsubscribe_events(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._event_subscribers.discard(queue)

    def publish_event(self, payload: dict[str, Any]) -> None:
        """Publish a backend-generated event to every Voice page subscriber."""
        for queue in tuple(self._event_subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(payload)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    def _ensure_monitor(self) -> None:
        if self._monitor_task and not self._monitor_task.done():
            return
        self._monitor_task = asyncio.create_task(self._monitor_turns())

    def _cancel_monitor(self) -> None:
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
        self._monitor_task = None

    async def _monitor_turns(self) -> None:
        try:
            import websockets
        except Exception as exc:
            self.last_error = str(exc)
            return
        while True:
            try:
                async with websockets.connect(
                    self.upstream_ws,
                    open_timeout=1.5,
                    ping_interval=None,
                    close_timeout=0.5,
                    proxy=None,
                ) as websocket:
                    self.last_error = None
                    self._service_ready_value = True
                    self._service_error = None
                    async for message in websocket:
                        payload = self._decode_json(message)
                        if payload is not None:
                            self._publish_upstream_event(payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                self._service_ready_value = False
                self._service_error = str(exc)
                await self._recover_broadcaster_if_needed()
                await asyncio.sleep(2)

    async def _recover_broadcaster_if_needed(self) -> None:
        if not (self.process and self.process.returncode is None):
            return
        now = time.monotonic()
        if now - self._last_broadcast_recovery < 10.0:
            return
        self._last_broadcast_recovery = now
        recovered = await asyncio.to_thread(self._start_broadcaster)
        if recovered:
            self.last_error = None

    async def forward_websocket(self, send_json) -> None:
        queue = await self.subscribe_events()
        try:
            await send_json({"type": "connection", "status": "connected"})
            while True:
                await send_json(await queue.get())
        finally:
            await self.unsubscribe_events(queue)

    def _publish_upstream_event(self, payload: dict[str, Any]) -> None:
        if payload.get("type") == "turn" and payload.get("text"):
            self._last_turn = payload
            self._ai_ready_at = time.time()
        elif payload.get("type") == "summary" and payload.get("text"):
            self._ai_ready_at = self._ai_ready_at or time.time()
        self.publish_event(payload)

    @staticmethod
    def _decode_json(message: str) -> dict[str, Any] | None:
        import json

        try:
            value = json.loads(message)
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def _device_ready(self, audio_devices: str) -> bool:
        key = audio_devices.lower()
        return any(token in key for token in ("xvf3800", "array", "respeaker"))

    def _start_broadcaster(self) -> bool:
        try:
            result = subprocess.run(
                [
                    "docker", "exec", "-d", "openvoicestream",
                    "/opt/venv/bin/python", "/tmp/ws_broadcast.py",
                    "--daemon", "--port", "8765", "--control-port", "8766",
                    "--idle-timeout", "0",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
            )
            return result.returncode == 0
        except Exception:
            return False
