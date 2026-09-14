from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


LOGGER = logging.getLogger("uvicorn.error")


class RuntimeState(StrEnum):
    DISABLED = "DISABLED"
    STARTING = "STARTING"
    WAITING_DEPENDENCY = "WAITING_DEPENDENCY"
    WAITING_DEVICE = "WAITING_DEVICE"
    READY = "READY"
    DEGRADED = "DEGRADED"
    RETRY_WAIT = "RETRY_WAIT"
    FAILED = "FAILED"
    DEGRADED_USB = "DEGRADED_USB"


@dataclass
class RuntimeStatus:
    name: str
    state: RuntimeState = RuntimeState.DISABLED
    phase: str = "DISABLED"
    attempts: int = 0
    last_error: str | None = None
    retry_in_seconds: float | None = None
    updated_at: str = ""
    ready_at: str | None = None
    fault_count: int = 0
    circuit_breaker_open: bool = False
    capture_health: dict[str, Any] = field(default_factory=dict)
    next_probe_at: str | None = None
    last_capture_fault: dict[str, Any] | None = None
    usb_recovery: dict[str, Any] | None = None

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["state"] = self.state.value
        return value


class RuntimeSupervisor:
    """Own optional AI runtimes without extending FastAPI readiness."""

    def __init__(
        self,
        *,
        sales_voice: Any,
        retail_single: Any,
        live_shelf: Any,
        hardware_detector: Any,
        backend_port: int = 8080,
        frontend_port: int = 5173,
        retry_schedule: tuple[float, ...] = (2.0, 5.0, 10.0, 20.0, 30.0),
        kernel_watchdog: bool = True,
        capture_stale_limit: int = 3,
        capture_poll_interval: float = 1.0,
    ):
        self.sales_voice = sales_voice
        self.retail_single = retail_single
        self.live_shelf = live_shelf
        self.hardware_detector = hardware_detector
        self.http_ports = tuple(port for port in (backend_port, frontend_port) if port > 0)
        self.retry_schedule = retry_schedule
        self.kernel_watchdog = kernel_watchdog
        self.capture_stale_limit = max(1, capture_stale_limit)
        self.capture_poll_interval = max(0.01, capture_poll_interval)
        self._records = {
            name: RuntimeStatus(name=name, updated_at=_utc_now())
            for name in ("hardware", "voice", "recamera", "multi")
        }
        self._bootstrap_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._stop_event = asyncio.Event()
        self._voice_wake = asyncio.Event()
        self._voice_language = "en"
        self._voice_probe_done = asyncio.Event()
        self._recamera_probe_done = asyncio.Event()
        self._voice_operation_lock = asyncio.Lock()
        self._voice_usb_fault_lock = asyncio.Lock()
        self._voice_inhibit_until = 0.0
        self._voice_fault_times: deque[float] = deque()
        self._voice_usb_faults = 0
        self._voice_circuit_breaker_open = False
        self._voice_next_probe_at: str | None = None
        self._voice_capture_healthy_since: float | None = None
        self._voice_capture_gate_seconds = 5.0
        self._voice_last_usb_warning_at: float | None = None
        self._voice_stale_checks = 0
        self._voice_last_capture_bytes: int | None = None
        self._voice_last_downmix_bytes: int | None = None
        self._voice_usb_recovery: dict[str, Any] | None = None

    def start(self) -> None:
        if self._bootstrap_task and not self._bootstrap_task.done():
            return
        self._stop_event.clear()
        for name in self._records:
            self._set(name, RuntimeState.STARTING, "WAITING_HTTP")
        self._bootstrap_task = asyncio.create_task(
            self._bootstrap(), name="runtime-supervisor-bootstrap"
        )

    async def stop(self) -> None:
        self._stop_event.set()
        self._voice_wake.set()
        tasks = list(self._tasks)
        if self._bootstrap_task:
            tasks.append(self._bootstrap_task)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._bootstrap_task = None

    def status(self) -> dict[str, Any]:
        return {
            "running": bool(self._bootstrap_task and not self._bootstrap_task.done()),
            "runtimes": {name: record.public_dict() for name, record in self._records.items()},
        }

    async def request_voice(self, language: str = "en") -> dict[str, Any]:
        if language not in {"zh", "en"}:
            raise ValueError("language must be zh or en")
        self._voice_language = language
        # A user request may wake dependency probing, but it must not erase a
        # USB fault from this boot session or silently reopen the breaker.
        self._voice_wake.set()
        return self.sales_voice.status()

    async def _bootstrap(self) -> None:
        try:
            await self._wait_for_http_services()
            LOGGER.info("[BOOT] Backend and frontend HTTP listeners ready")

            self._spawn(self._hardware_loop(), "runtime-supervisor-hardware")
            self._spawn(self._voice_loop(), "runtime-supervisor-voice")
            if self.kernel_watchdog:
                self._spawn(self._voice_usb_watchdog_loop(), "runtime-supervisor-voice-usb")
            await self._voice_probe_done.wait()

            self._spawn(self._recamera_loop(), "runtime-supervisor-recamera")
            await self._recamera_probe_done.wait()

            try:
                await self.live_shelf.prepare()
                self._set("multi", RuntimeState.READY, "PREPARED_IDLE")
                LOGGER.info("[MULTI] Prepared metadata; inference remains idle")
            except Exception as exc:
                self._set("multi", RuntimeState.DEGRADED, "PREPARE_FAILED", error=str(exc))

            await self._stop_event.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.exception("[BOOT] Runtime supervisor bootstrap failed")
            for name in self._records:
                if self._records[name].state != RuntimeState.READY:
                    self._set(name, RuntimeState.FAILED, "SUPERVISOR_FAILED", error=str(exc))

    async def _wait_for_http_services(self) -> None:
        for port in self.http_ports:
            while not self._stop_event.is_set():
                try:
                    _reader, writer = await asyncio.wait_for(
                        asyncio.open_connection("127.0.0.1", port), timeout=0.5
                    )
                    writer.close()
                    await writer.wait_closed()
                    break
                except (OSError, asyncio.TimeoutError):
                    await self._pause(0.5)

    async def _hardware_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.hardware_detector.start()
                self._set("hardware", RuntimeState.READY, "MONITORING")
                await self._pause(10.0)
                if not self.hardware_detector.status().get("running"):
                    self._set("hardware", RuntimeState.DEGRADED, "MONITOR_STOPPED")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._set("hardware", RuntimeState.RETRY_WAIT, "START_FAILED", error=str(exc))
                await self._pause(10.0)

    async def _voice_loop(self) -> None:
        failures = 0
        websocket_failures = 0
        while not self._stop_event.is_set():
            try:
                if self._voice_circuit_breaker_open:
                    self._set_voice(
                        RuntimeState.DEGRADED_USB,
                        "DEGRADED_USB",
                        error="Voice USB circuit breaker is open for this boot session",
                        capture_health=self._capture_health_snapshot(),
                    )
                    await self._voice_pause(30.0)
                    continue

                retry_remaining = self._voice_inhibit_until - asyncio.get_running_loop().time()
                if retry_remaining > 0:
                    recovery = self._voice_usb_recovery
                    if recovery:
                        recovered = bool(recovery.get("ok"))
                        phase = (
                            "USB_RECOVERY_COOLDOWN"
                            if recovered
                            else "USB_RECOVERY_UNAVAILABLE"
                        )
                        error = (
                            "XVF3800 USB reset completed; waiting for the device to settle"
                            if recovered
                            else "XVF3800 USB reset was unavailable; using protected retry"
                        )
                    else:
                        phase = "USB_CAPTURE_COOLDOWN"
                        error = "RK3588 xHCI overrun burst detected"
                    self._set(
                        "voice", RuntimeState.RETRY_WAIT, phase,
                        attempts=self._voice_usb_faults,
                        error=error,
                        retry=round(retry_remaining, 1),
                    )
                    self._update_voice_meta(
                        next_probe_at=self._voice_next_probe_at,
                        capture_health=self._capture_health_snapshot(),
                    )
                    await self._voice_pause(min(retry_remaining, 30.0))
                    continue
                self._voice_next_probe_at = None

                docker_ready, docker_error = await self.sales_voice.probe_docker()
                if not docker_ready:
                    failures += 1
                    delay = self._retry_delay(failures)
                    self._set(
                        "voice", RuntimeState.WAITING_DEPENDENCY, "WAITING_DOCKER",
                        attempts=failures, error=docker_error, retry=delay,
                    )
                    self._voice_probe_done.set()
                    await self._voice_pause(delay)
                    continue

                device_ready, device_error = await self.sales_voice.probe_device()
                if not device_ready:
                    failures += 1
                    delay = self._retry_delay(failures)
                    self._set(
                        "voice", RuntimeState.WAITING_DEVICE, "WAITING_RESPEAKER",
                        attempts=failures, error=device_error, retry=delay,
                    )
                    self._voice_probe_done.set()
                    await self._voice_pause(delay)
                    continue

                self._voice_probe_done.set()
                running = bool(self.sales_voice.status().get("running"))
                if not running:
                    model_probe = getattr(self.sales_voice, "probe_model_services", None)
                    model_health = await model_probe() if callable(model_probe) else {
                        "stt_ready": True,
                        "llm_ready": True,
                    }
                    if not model_health.get("stt_ready") or not model_health.get("llm_ready"):
                        missing = []
                        if not model_health.get("stt_ready"):
                            missing.append("STT")
                        if not model_health.get("llm_ready"):
                            missing.append("LLM")
                        self._set_voice(
                            RuntimeState.STARTING,
                            "WAITING_MODEL_SERVICES",
                            attempts=failures,
                            error=(
                                f"Waiting for {' and '.join(missing)} model service readiness "
                                "before starting USB capture"
                            ),
                            retry=2.0,
                        )
                        await self._voice_pause(2.0)
                        continue
                    voice_record = self._records["voice"]
                    capture_expected = (
                        voice_record.state in {RuntimeState.READY, RuntimeState.DEGRADED}
                        or voice_record.phase in {
                            "CAPTURE_READY",
                            "STARTING_ASR",
                            "WAITING_WEBSOCKET_8765",
                        }
                    )
                    if capture_expected:
                        failures += 1
                        await self._record_capture_fault("CAPTURE_PROCESS_EXITED")
                        self._set_voice(
                            RuntimeState.DEGRADED,
                            "CAPTURE_PROCESS_EXITED",
                            attempts=failures,
                            error="Owned Voice run.sh process exited",
                            capture_health=self._capture_health_snapshot(),
                        )
                        async with self._voice_operation_lock:
                            await self.sales_voice.stop_runtime()
                        await self._record_capture_fault_result("CAPTURE_PROCESS_EXITED")
                        delay = self._retry_delay(failures)
                        self._set_voice(
                            RuntimeState.RETRY_WAIT,
                            "CAPTURE_RESTART_PENDING",
                            attempts=failures,
                            error="Recovery pending after CAPTURE_PROCESS_EXITED",
                            retry=delay,
                            capture_health=self._capture_health_snapshot(),
                        )
                        await self._voice_pause(delay)
                        continue
                    self._voice_capture_healthy_since = None
                    self._reset_capture_samples()
                    self._set("voice", RuntimeState.STARTING, "STARTING_CAPTURE", attempts=failures)
                    async with self._voice_operation_lock:
                        await self.sales_voice.start(language=self._voice_language)
                    websocket_failures = 0

                await self._refresh_capture_diagnostics()
                capture_health = self._capture_health_snapshot()
                if not capture_health.get("healthy"):
                    self._voice_capture_healthy_since = None
                    failure_kind = capture_health.get("failure_kind")
                    process_exited = failure_kind == "CAPTURE_PROCESS_EXITED"
                    self._voice_stale_checks = 0 if process_exited else self._voice_stale_checks + 1
                    voice_record = self._records["voice"]
                    observing_active_capture = (
                        voice_record.state == RuntimeState.READY
                        or voice_record.phase == "DEGRADED_CAPTURE"
                    )
                    if observing_active_capture or process_exited:
                        if not process_exited and self._voice_stale_checks < self.capture_stale_limit:
                            self._set_voice(
                                RuntimeState.DEGRADED,
                                "DEGRADED_CAPTURE",
                                attempts=self._voice_stale_checks,
                                error="Voice heartbeat temporarily stale; processes remain under observation",
                                retry=self.capture_poll_interval,
                                capture_health=capture_health,
                            )
                            await self._voice_pause(self.capture_poll_interval)
                            continue
                        failures += 1
                        delay = self._retry_delay(failures)
                        phase = "CAPTURE_PROCESS_EXITED" if process_exited else "CAPTURE_HEARTBEAT_STALE"
                        await self._record_capture_fault(phase)
                        self._set_voice(
                            RuntimeState.DEGRADED,
                            phase,
                            attempts=failures,
                            error=(
                                "Owned Voice pipeline process exited"
                                if process_exited
                                else "Voice heartbeat remained stale for three consecutive checks"
                            ),
                            capture_health=capture_health,
                        )
                        async with self._voice_operation_lock:
                            await self.sales_voice.stop_runtime()
                        await self._record_capture_fault_result(phase)
                        self._set_voice(
                            RuntimeState.RETRY_WAIT,
                            "CAPTURE_RESTART_PENDING",
                            attempts=failures,
                            error=f"Recovery pending after {phase}",
                            retry=delay,
                            capture_health=self._capture_health_snapshot(),
                        )
                        await self._voice_pause(delay)
                        continue
                    self._set_voice(
                        RuntimeState.STARTING,
                        "STARTING_CAPTURE",
                        attempts=failures,
                        error="Waiting for valid XVF3800 audio heartbeat",
                        retry=self.capture_poll_interval,
                        capture_health=capture_health,
                    )
                    await self._voice_pause(self.capture_poll_interval)
                    continue

                self._voice_stale_checks = 0
                heartbeat_growing = self._capture_heartbeats_growing(capture_health)
                if capture_health.get("gate_required", True):
                    now = asyncio.get_running_loop().time()
                    if not heartbeat_growing:
                        self._voice_capture_healthy_since = None
                        self._set_voice(
                            RuntimeState.STARTING,
                            "STARTING_CAPTURE",
                            attempts=failures,
                            error="Waiting for sustained capture and downmix heartbeat growth",
                            retry=self.capture_poll_interval,
                            capture_health=capture_health,
                        )
                        await self._voice_pause(self.capture_poll_interval)
                        continue
                    if self._voice_capture_healthy_since is None:
                        self._voice_capture_healthy_since = now
                    capture_age = now - self._voice_capture_healthy_since
                    if capture_age < self._voice_capture_gate_seconds:
                        self._set_voice(
                            RuntimeState.STARTING,
                            "CAPTURE_READY",
                            attempts=failures,
                            retry=round(self._voice_capture_gate_seconds - capture_age, 1),
                            capture_health=capture_health,
                        )
                        await self._voice_pause(min(1.0, self._voice_capture_gate_seconds - capture_age))
                        continue

                if self._records["voice"].state != RuntimeState.READY:
                    self._set_voice(
                        RuntimeState.STARTING,
                        "STARTING_ASR",
                        attempts=failures,
                        capture_health=capture_health,
                    )
                websocket_ready, websocket_error = await self.sales_voice.probe_websocket()
                if websocket_ready:
                    model_probe = getattr(self.sales_voice, "probe_model_services", None)
                    model_health = await model_probe() if callable(model_probe) else {
                        "stt_ready": True,
                        "llm_ready": True,
                    }
                    if not model_health.get("stt_ready") or not model_health.get("llm_ready"):
                        missing = []
                        if not model_health.get("stt_ready"):
                            missing.append("STT")
                        if not model_health.get("llm_ready"):
                            missing.append("LLM")
                        self._set_voice(
                            RuntimeState.STARTING,
                            "WAITING_MODEL_SERVICES",
                            attempts=failures,
                            error=f"Waiting for {' and '.join(missing)} model service readiness",
                            retry=2.0,
                            capture_health=capture_health,
                        )
                        await self._voice_pause(2.0)
                        continue
                    failures = 0
                    websocket_failures = 0
                    self._set_voice(
                        RuntimeState.READY,
                        "WEBSOCKET_8765_READY",
                        capture_health=capture_health,
                    )
                    await self._voice_pause(self.capture_poll_interval)
                    continue

                websocket_failures += 1
                process_running = bool(self.sales_voice.status().get("running"))
                if process_running and websocket_failures < 6:
                    self._set(
                        "voice", RuntimeState.STARTING, "WAITING_WEBSOCKET_8765",
                        attempts=websocket_failures, error=websocket_error, retry=2.0,
                    )
                    await self._voice_pause(2.0)
                    continue

                if process_running:
                    self._set(
                        "voice", RuntimeState.DEGRADED, "WEBSOCKET_8765_UNAVAILABLE",
                        attempts=websocket_failures, error=websocket_error,
                    )
                    async with self._voice_operation_lock:
                        await self.sales_voice.stop_runtime()

                failures += 1
                delay = self._retry_delay(failures)
                self._set(
                    "voice", RuntimeState.RETRY_WAIT, "RESTART_PENDING",
                    attempts=failures, error=websocket_error, retry=delay,
                )
                await self._voice_pause(delay)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._voice_probe_done.set()
                failures += 1
                delay = self._retry_delay(failures)
                self._set(
                    "voice", RuntimeState.RETRY_WAIT, "START_FAILED",
                    attempts=failures, error=str(exc), retry=delay,
                )
                await self._voice_pause(delay)

    async def _voice_usb_watchdog_loop(self) -> None:
        pattern = "xhci-hcd xhci-hcd.12.auto: WARN: buffer overrun event"
        while not self._stop_event.is_set():
            if not self.sales_voice.status().get("running"):
                await self._pause(1.0)
                continue

            process: asyncio.subprocess.Process | None = None
            triggered = False
            try:
                process = await asyncio.create_subprocess_exec(
                    "journalctl", "-k", "-f", "-n", "0", "--no-pager",
                    "-o", "cat", "-g", pattern,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                timestamps: deque[float] = deque()
                while not self._stop_event.is_set() and self.sales_voice.status().get("running"):
                    try:
                        line = await asyncio.wait_for(process.stdout.readline(), timeout=2.0)
                    except asyncio.TimeoutError:
                        if process.returncode is not None:
                            break
                        continue
                    if not line:
                        break
                    now = asyncio.get_running_loop().time()
                    self._voice_last_usb_warning_at = now
                    timestamps.append(now)
                    while timestamps and now - timestamps[0] > 2.0:
                        timestamps.popleft()
                    if len(timestamps) >= 20:
                        triggered = True
                        await self._handle_voice_usb_fault()
                        break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning("[VOICE] USB watchdog unavailable: %s", exc)
            finally:
                if process and process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.wait()
            await self._pause(1.0 if triggered else 30.0)

    async def _handle_voice_usb_fault(self) -> None:
        async with self._voice_usb_fault_lock:
            if self._voice_circuit_breaker_open:
                return
            await self._handle_voice_usb_fault_locked()

    async def _handle_voice_usb_fault_locked(self) -> None:
        now = asyncio.get_running_loop().time()
        while self._voice_fault_times and now - self._voice_fault_times[0] > 300.0:
            self._voice_fault_times.popleft()
        self._voice_fault_times.append(now)
        self._voice_usb_faults += 1
        fault_count = len(self._voice_fault_times)
        self._voice_capture_healthy_since = None
        self._reset_capture_samples()
        if fault_count >= 2:
            self._voice_circuit_breaker_open = True
            self._voice_inhibit_until = 0.0
            self._voice_next_probe_at = None
            self._set_voice(
                RuntimeState.DEGRADED_USB,
                "DEGRADED_USB",
                attempts=self._voice_usb_faults,
                error="Repeated RK3588 xHCI overrun bursts; automatic Voice restart disabled",
                capture_health=self._capture_health_snapshot(),
            )
        else:
            # Block the Voice loop before stopping capture so it cannot race
            # the one-shot, device-scoped recovery operation.
            cooldown = 60.0
            self._voice_inhibit_until = now + cooldown
            self._voice_next_probe_at = _utc_after_seconds(cooldown)
            self._set_voice(
                RuntimeState.DEGRADED,
                "USB_CAPTURE_OVERRUN",
                attempts=self._voice_usb_faults,
                error="RK3588 xHCI overrun burst detected; stopping owned Voice capture",
                retry=cooldown,
                capture_health=self._capture_health_snapshot(),
                next_probe_at=self._voice_next_probe_at,
            )
        await self._record_capture_fault("USB_CAPTURE_OVERRUN")
        async with self._voice_operation_lock:
            await self.sales_voice.stop_runtime()
            if fault_count == 1:
                recover = getattr(self.sales_voice, "recover_usb_device", None)
                if callable(recover):
                    self._set_voice(
                        RuntimeState.DEGRADED,
                        "RECOVERING_XVF3800_USB",
                        attempts=self._voice_usb_faults,
                        error="Requesting one device-scoped XVF3800 USB reset",
                        capture_health=self._capture_health_snapshot(),
                    )
                    try:
                        self._voice_usb_recovery = await recover()
                    except Exception as exc:
                        self._voice_usb_recovery = {
                            "ok": False,
                            "error": str(exc),
                        }

        await self._record_capture_fault_result("USB_CAPTURE_OVERRUN")

        if fault_count == 1:
            cooldown = 30.0
            self._voice_inhibit_until = asyncio.get_running_loop().time() + cooldown
            self._voice_next_probe_at = _utc_after_seconds(cooldown)
            recovered = bool(
                self._voice_usb_recovery and self._voice_usb_recovery.get("ok")
            )
            self._set_voice(
                RuntimeState.RETRY_WAIT,
                "USB_RECOVERY_COOLDOWN" if recovered else "USB_RECOVERY_UNAVAILABLE",
                attempts=self._voice_usb_faults,
                error=(
                    "XVF3800 USB reset completed; waiting for the device to settle"
                    if recovered
                    else "XVF3800 USB reset was unavailable; using protected retry"
                ),
                retry=cooldown,
                capture_health=self._capture_health_snapshot(),
                next_probe_at=self._voice_next_probe_at,
            )
        self._voice_wake.set()

    async def _refresh_capture_diagnostics(self) -> None:
        refresh = getattr(self.sales_voice, "refresh_capture_diagnostics", None)
        if callable(refresh):
            await refresh()

    async def _record_capture_fault(self, reason: str) -> None:
        refresh = getattr(self.sales_voice, "refresh_capture_diagnostics", None)
        if callable(refresh):
            diagnostics = await refresh(
                force=True,
                include_services=True,
                persist_reason=reason,
            )
            self._records["voice"].last_capture_fault = {
                "reason": reason,
                "before_stop": diagnostics,
            }

    async def _record_capture_fault_result(self, reason: str) -> None:
        persist = getattr(self.sales_voice, "persist_capture_fault_result", None)
        if callable(persist):
            diagnostics = await persist(reason)
            current = self._records["voice"].last_capture_fault or {"reason": reason}
            current["after_stop"] = diagnostics
            self._records["voice"].last_capture_fault = current

    def _capture_heartbeats_growing(self, health: dict[str, Any]) -> bool:
        capture_total = health.get("capture_heartbeat", {}).get("bytes_total")
        downmix_total = health.get("downmix_heartbeat", {}).get("bytes_total")
        growing = (
            isinstance(capture_total, int)
            and isinstance(downmix_total, int)
            and self._voice_last_capture_bytes is not None
            and self._voice_last_downmix_bytes is not None
            and capture_total > self._voice_last_capture_bytes
            and downmix_total > self._voice_last_downmix_bytes
        )
        if isinstance(capture_total, int):
            self._voice_last_capture_bytes = capture_total
        if isinstance(downmix_total, int):
            self._voice_last_downmix_bytes = downmix_total
        return growing

    def _reset_capture_samples(self) -> None:
        self._voice_stale_checks = 0
        self._voice_last_capture_bytes = None
        self._voice_last_downmix_bytes = None

    def _capture_health_snapshot(self) -> dict[str, Any]:
        health = getattr(self.sales_voice, "capture_health", None)
        if callable(health):
            try:
                snapshot = dict(health())
                warning_age = (
                    None
                    if self._voice_last_usb_warning_at is None
                    else max(0.0, time.monotonic() - self._voice_last_usb_warning_at)
                )
                xhci_quiet = warning_age is None or warning_age >= self._voice_capture_gate_seconds
                snapshot["xhci_quiet"] = xhci_quiet
                snapshot["last_xhci_warning_age_seconds"] = (
                    round(warning_age, 3) if warning_age is not None else None
                )
                snapshot["healthy"] = bool(snapshot.get("healthy")) and xhci_quiet
                return snapshot
            except Exception as exc:
                return {"healthy": False, "error": str(exc)}
        # Test doubles and legacy adapters without a heartbeat remain usable;
        # the production adapter always supplies the stricter health check.
        return {
            "healthy": bool(self.sales_voice.status().get("running")),
            "heartbeat_available": False,
            "gate_required": False,
        }

    def _set_voice(self, state: RuntimeState, phase: str, **kwargs: Any) -> None:
        capture_health = kwargs.pop("capture_health", None)
        next_probe_at = kwargs.pop("next_probe_at", self._voice_next_probe_at)
        self._set("voice", state, phase, **kwargs)
        self._update_voice_meta(
            fault_count=self._voice_usb_faults,
            circuit_breaker_open=self._voice_circuit_breaker_open,
            capture_health=capture_health or self._capture_health_snapshot(),
            next_probe_at=next_probe_at,
        )
        self._records["voice"].usb_recovery = self._voice_usb_recovery

    def _update_voice_meta(
        self,
        *,
        fault_count: int | None = None,
        circuit_breaker_open: bool | None = None,
        capture_health: dict[str, Any] | None = None,
        next_probe_at: str | None = None,
    ) -> None:
        record = self._records["voice"]
        if fault_count is not None:
            record.fault_count = fault_count
        if circuit_breaker_open is not None:
            record.circuit_breaker_open = circuit_breaker_open
        if capture_health is not None:
            record.capture_health = capture_health
        record.next_probe_at = next_probe_at

    async def _recamera_loop(self) -> None:
        failures = 0
        rtsp_url = self.retail_single.source_url
        while not self._stop_event.is_set():
            try:
                await self.retail_single.ensure_prewarm(url=rtsp_url)
                status = self.retail_single.prewarm_status()
                self._recamera_probe_done.set()
                if not status.get("running"):
                    raise RuntimeError("reCamera capture worker exited")
                if not status.get("network_reachable"):
                    failures += 1
                    delay = self._retry_delay(failures)
                    self._set(
                        "recamera", RuntimeState.WAITING_DEPENDENCY, "OFFLINE",
                        attempts=failures, error=status.get("last_error"), retry=delay,
                    )
                    await self._pause(delay)
                    continue
                if not status.get("rtsp_port_ready"):
                    failures += 1
                    delay = self._retry_delay(failures)
                    self._set(
                        "recamera", RuntimeState.WAITING_DEPENDENCY, "NETWORK_READY",
                        attempts=failures, error=status.get("last_error"), retry=delay,
                    )
                    await self._pause(delay)
                    continue
                if not status.get("connected"):
                    self._set(
                        "recamera", RuntimeState.STARTING, "STREAM_WAITING",
                        attempts=int(status.get("connection_attempts", 0)),
                        error=status.get("last_error"), retry=10.0,
                    )
                    await self._pause(10.0)
                    continue

                failures = 0
                websocket_ready = bool(self.retail_single.status().get("websocket_connected"))
                if websocket_ready:
                    self._set("recamera", RuntimeState.READY, "WS_READY")
                else:
                    self._set("recamera", RuntimeState.DEGRADED, "STREAM_READY")
                await self._pause(10.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._recamera_probe_done.set()
                failures += 1
                delay = self._retry_delay(failures)
                self._set(
                    "recamera", RuntimeState.RETRY_WAIT, "WORKER_RESTART_PENDING",
                    attempts=failures, error=str(exc), retry=delay,
                )
                await self._pause(delay)

    def _spawn(self, coroutine, name: str) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _set(
        self,
        name: str,
        state: RuntimeState,
        phase: str,
        *,
        attempts: int = 0,
        error: str | None = None,
        retry: float | None = None,
    ) -> None:
        record = self._records[name]
        changed = (record.state, record.phase, record.last_error) != (state, phase, error)
        record.state = state
        record.phase = phase
        record.attempts = attempts
        record.last_error = error
        record.retry_in_seconds = retry
        record.updated_at = _utc_now()
        if state == RuntimeState.READY and record.ready_at is None:
            record.ready_at = record.updated_at
        if changed:
            LOGGER.info("[%s] state=%s phase=%s error=%s", name.upper(), state.value, phase, error)

    def _retry_delay(self, failures: int) -> float:
        if not self.retry_schedule:
            return 10.0
        index = min(max(failures - 1, 0), len(self.retry_schedule) - 1)
        return self.retry_schedule[index]

    async def _pause(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _voice_pause(self, seconds: float) -> None:
        stop_task = asyncio.create_task(self._stop_event.wait())
        wake_task = asyncio.create_task(self._voice_wake.wait())
        try:
            done, _pending = await asyncio.wait(
                {stop_task, wake_task}, timeout=seconds, return_when=asyncio.FIRST_COMPLETED
            )
            if wake_task in done:
                self._voice_wake.clear()
        finally:
            for task in (stop_task, wake_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(stop_task, wake_task, return_exceptions=True)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_after_seconds(seconds: float) -> str:
    from datetime import timedelta

    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
