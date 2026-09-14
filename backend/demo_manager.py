from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.domain import ACTIVE_STATES, ConflictError, RunRecord, utc_now
from app.process_manager import ProcessManager


class DemoState(str, Enum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    ERROR = "ERROR"


@dataclass
class SwitchStatus:
    state: DemoState = DemoState.STOPPED
    current_demo_id: str | None = None
    current_run_id: str | None = None
    target_demo_id: str | None = None
    previous_demo_id: str | None = None
    error: str | None = None
    updated_at: str = ""

    def public_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "current_demo_id": self.current_demo_id,
            "current_run_id": self.current_run_id,
            "target_demo_id": self.target_demo_id,
            "previous_demo_id": self.previous_demo_id,
            "error": self.error,
            "updated_at": self.updated_at,
        }


class DemoSwitchError(ConflictError):
    """Raised when a coordinated demo switch cannot complete safely."""


class DemoManager:
    """Serializes stop-cleanup-start transitions across all registered demos."""

    def __init__(self, process_manager: ProcessManager):
        self.process_manager = process_manager
        self.status = SwitchStatus(updated_at=utc_now())
        self._switch_lock = asyncio.Lock()

    async def switch_demo(
        self,
        target_demo_id: str,
        parameters: dict[str, Any] | None = None,
    ) -> SwitchStatus:
        # Validate the target before interrupting a healthy current demo.
        target_manifest = self.process_manager.registry.get(target_demo_id)
        async with self._switch_lock:
            active = self._active_runs()
            same_target = next((run for run in active if run.demo_id == target_demo_id), None)
            if same_target and len(active) == 1:
                self._set_status(
                    DemoState.RUNNING if same_target.state == "running" else DemoState.STARTING,
                    current_demo_id=target_demo_id,
                    current_run_id=same_target.run_id,
                    target_demo_id=target_demo_id,
                )
                return self.status

            previous_demo_id = active[0].demo_id if active else self.status.current_demo_id
            self._set_status(
                DemoState.STOPPING if active else DemoState.STOPPED,
                current_demo_id=previous_demo_id,
                current_run_id=active[0].run_id if active else None,
                target_demo_id=target_demo_id,
                previous_demo_id=previous_demo_id,
            )

            try:
                for run in active:
                    await self.process_manager.stop(run.run_id)
                    await self._wait_until_terminal(run, self._stop_wait_seconds(run))
                    # ProcessManager normally releases this in _supervise; release is idempotent
                    # and guarantees cleanup before the next demo acquires resources.
                    await self.process_manager.resources.release(run.run_id)

                self._set_status(
                    DemoState.STOPPED,
                    target_demo_id=target_demo_id,
                    previous_demo_id=previous_demo_id,
                )
                self._set_status(
                    DemoState.STARTING,
                    target_demo_id=target_demo_id,
                    previous_demo_id=previous_demo_id,
                )
                target_run = await self.process_manager.start(target_demo_id, parameters or {})
                self.status.current_demo_id = target_demo_id
                self.status.current_run_id = target_run.run_id
                self.status.updated_at = utc_now()
                await self._wait_until_ready(
                    target_run,
                    float(target_manifest.lifecycle.get("start_timeout_sec", 60)),
                )
                self._set_status(
                    DemoState.RUNNING,
                    current_demo_id=target_demo_id,
                    current_run_id=target_run.run_id,
                    target_demo_id=target_demo_id,
                    previous_demo_id=previous_demo_id,
                )
                return self.status
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._cleanup_failed_target(target_demo_id)
                self._set_status(
                    DemoState.ERROR,
                    target_demo_id=target_demo_id,
                    previous_demo_id=previous_demo_id,
                    error=str(exc),
                )
                if isinstance(exc, DemoSwitchError):
                    raise
                raise DemoSwitchError(f"failed to switch to {target_demo_id}: {exc}") from exc

    def get_status(self) -> dict[str, Any]:
        self._reconcile()
        return self.status.public_dict()

    def _active_runs(self) -> list[RunRecord]:
        return sorted(
            (run for run in self.process_manager.runs.values() if run.state in ACTIVE_STATES),
            key=lambda run: run.created_at,
        )

    async def _wait_until_terminal(self, run: RunRecord, timeout: float) -> None:
        try:
            async with asyncio.timeout(timeout):
                while self.process_manager.get(run.run_id).state in ACTIVE_STATES:
                    await asyncio.sleep(0.05)
        except TimeoutError as exc:
            raise DemoSwitchError(f"timeout stopping {run.demo_id}/{run.run_id}") from exc

    async def _wait_until_ready(self, run: RunRecord, timeout: float) -> None:
        try:
            async with asyncio.timeout(timeout):
                while True:
                    current = self.process_manager.get(run.run_id)
                    if current.state == "running" and current.ready:
                        return
                    if current.state in {"failed", "completed", "stopped"}:
                        raise DemoSwitchError(
                            f"target {run.demo_id} exited before ready: "
                            f"state={current.state}, error={current.last_error or 'none'}"
                        )
                    await asyncio.sleep(0.05)
        except TimeoutError as exc:
            raise DemoSwitchError(f"timeout starting {run.demo_id}/{run.run_id}") from exc

    async def _cleanup_failed_target(self, target_demo_id: str) -> None:
        for run in self._active_runs():
            if run.demo_id != target_demo_id:
                continue
            try:
                await self.process_manager.stop(run.run_id)
                await self._wait_until_terminal(run, self._stop_wait_seconds(run))
            except Exception:
                # Preserve the original switch exception. ProcessManager already escalates to
                # SIGKILL on its own stop timeout.
                pass
            finally:
                await self.process_manager.resources.release(run.run_id)

    def _stop_wait_seconds(self, run: RunRecord) -> float:
        manifest = self.process_manager.registry.get(run.demo_id)
        return float(manifest.lifecycle.get("stop_timeout_sec", 20)) + 5.0

    def _reconcile(self) -> None:
        if self._switch_lock.locked():
            return
        active = self._active_runs()
        if active:
            run = active[0]
            state = DemoState.RUNNING if run.state == "running" and run.ready else DemoState.STARTING
            self._set_status(
                state,
                current_demo_id=run.demo_id,
                current_run_id=run.run_id,
                target_demo_id=run.demo_id,
            )
        elif self.status.state not in {DemoState.ERROR, DemoState.STOPPED}:
            self._set_status(DemoState.STOPPED)

    def _set_status(
        self,
        state: DemoState,
        *,
        current_demo_id: str | None = None,
        current_run_id: str | None = None,
        target_demo_id: str | None = None,
        previous_demo_id: str | None = None,
        error: str | None = None,
    ) -> None:
        self.status = SwitchStatus(
            state=state,
            current_demo_id=current_demo_id,
            current_run_id=current_run_id,
            target_demo_id=target_demo_id,
            previous_demo_id=previous_demo_id,
            error=error,
            updated_at=utc_now(),
        )

