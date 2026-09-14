from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable


LOGGER = logging.getLogger("demo_hub.showcase")


@dataclass
class ShowcaseStatus:
    active_demo: str | None = None
    target_demo: str | None = None
    state: str = "IDLE"
    generation: int = 0
    error: str | None = None
    started_at: str | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "active_demo": self.active_demo,
            "target_demo": self.target_demo,
            "state": self.state,
            "generation": self.generation,
            "error": self.error,
            "started_at": self.started_at,
        }


class StaleSwitch(Exception):
    pass


class ShowcaseSwitchController:
    VALID_DEMOS = {"single", "multi", "voice", "none"}

    def __init__(
        self,
        *,
        live_shelf: Any,
        sales_voice: Any,
        recamera: Any,
        retail_single: Any,
        process_manager: Any,
        runtime_supervisor: Any | None = None,
        inventory_voice: Any | None = None,
        visual_cleanup: Callable[[], Awaitable[None]] | None = None,
    ):
        self.live_shelf = live_shelf
        self.sales_voice = sales_voice
        self.recamera = recamera
        self.retail_single = retail_single
        self.process_manager = process_manager
        self.runtime_supervisor = runtime_supervisor
        self.inventory_voice = inventory_voice
        self.visual_cleanup = visual_cleanup
        self.status = ShowcaseStatus()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    async def switch(self, demo: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
        if demo not in self.VALID_DEMOS:
            raise ValueError("demo must be one of: single, multi, voice, none")

        parameters = parameters or {}
        async with self._lock:
            self.status.generation += 1
            generation = self.status.generation
            old_demo = self.status.active_demo
            self.status.target_demo = None if demo == "none" else demo
            self.status.state = "SWITCHING"
            self.status.error = None
            self.status.started_at = _utc_now()

            if self._task and not self._task.done():
                self._task.cancel()

            self._task = asyncio.create_task(
                self._run_switch(generation, old_demo, demo, parameters),
                name=f"showcase-switch-{generation}-{demo}",
            )

            LOGGER.info(
                "[SWITCH] old_demo=%s new_demo=%s generation=%s state=SWITCHING",
                old_demo,
                demo,
                generation,
            )
            return self.status.public_dict()

    async def stop(self) -> dict[str, Any]:
        return await self.switch("none", {})

    def get_status(self) -> dict[str, Any]:
        self._refresh_runtime_health()
        return self.status.public_dict()

    async def wait_for_idle(self, timeout: float = 30.0) -> dict[str, Any]:
        task = self._task
        if task and not task.done():
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        return self.get_status()

    async def _run_switch(
        self,
        generation: int,
        old_demo: str | None,
        target: str,
        parameters: dict[str, Any],
    ) -> None:
        switch_started = asyncio.get_running_loop().time()
        stop_started = switch_started
        try:
            self._ensure_current(generation)
            await self._set_state(generation, "STOPPING")
            await self._stop_all()

            stop_done = asyncio.get_running_loop().time()
            self._ensure_current(generation)

            if target == "none":
                await self._complete(generation, None, "IDLE")
                LOGGER.info(
                    "[SWITCH] old_demo=%s new_demo=%s generation=%s stop_start_time=%.3fs ready_time=0.000s final_state=IDLE",
                    old_demo,
                    target,
                    generation,
                    stop_done - stop_started,
                )
                return

            await self._set_state(generation, "STARTING")
            await self._start_target(target, parameters)

            self._ensure_current(generation)
            await self._set_state(generation, "READY_CHECK")
            await self._wait_ready(generation, target)
            self._ensure_current(generation)
            await asyncio.sleep(1)
            self._ensure_current(generation)
            if not await self._is_ready(target):
                raise RuntimeError(f"{target} readiness was not stable")

            ready_done = asyncio.get_running_loop().time()
            await self._complete(generation, target, "RUNNING")
            LOGGER.info(
                "[SWITCH] old_demo=%s new_demo=%s generation=%s stop_start_time=%.3fs ready_time=%.3fs final_state=RUNNING",
                old_demo,
                target,
                generation,
                stop_done - stop_started,
                ready_done - stop_done,
            )
        except asyncio.CancelledError:
            LOGGER.info(
                "[SWITCH] old_demo=%s new_demo=%s generation=%s final_state=CANCELLED",
                old_demo,
                target,
                generation,
            )
        except StaleSwitch:
            LOGGER.info(
                "[SWITCH] old_demo=%s new_demo=%s generation=%s final_state=STALE",
                old_demo,
                target,
                generation,
            )
        except Exception as exc:
            await self._fail_if_current(generation, exc)
            LOGGER.exception(
                "[SWITCH] old_demo=%s new_demo=%s generation=%s final_state=ERROR",
                old_demo,
                target,
                generation,
            )

    async def _stop_all(self) -> None:
        if self.inventory_voice is not None:
            self.inventory_voice.set_enabled(False)
        for adapter in (self.sales_voice, self.retail_single, self.live_shelf):
            setter = getattr(adapter, "set_display_active", None)
            if callable(setter):
                setter(False)
            else:
                stop = getattr(adapter, "stop", None)
                if callable(stop):
                    await stop()

    async def _start_target(self, target: str, parameters: dict[str, Any]) -> None:
        if target == "voice":
            if hasattr(self.sales_voice, "set_display_active"):
                self.sales_voice.set_display_active(True)
            status = self.sales_voice.status()
            if not status.get("running"):
                if self.runtime_supervisor is not None:
                    await self.runtime_supervisor.request_voice(parameters.get("language", "en"))
                else:
                    await self.sales_voice.start(language=parameters.get("language", "en"))
        elif target == "multi":
            if hasattr(self.live_shelf, "set_display_active"):
                self.live_shelf.set_display_active(True)
            if not self.live_shelf.status().get("running"):
                await self.live_shelf.start()
        elif target == "single":
            if self.inventory_voice is not None:
                self.inventory_voice.set_enabled(True)
            # The single-camera experience includes the Voice Inventory
            # Assistant, so expose the shared Voice runtime as display-active
            # just like the standalone voice demo.
            if hasattr(self.sales_voice, "set_display_active"):
                self.sales_voice.set_display_active(True)
            if self.runtime_supervisor is not None:
                await self.runtime_supervisor.request_voice(parameters.get("language", "en"))
            url = parameters.get("url") if isinstance(parameters.get("url"), str) else None
            if hasattr(self.retail_single, "set_display_active"):
                self.retail_single.set_display_active(True)
            if url and hasattr(self.retail_single, "ensure_prewarm"):
                await self.retail_single.ensure_prewarm(url=url)
            if not self.retail_single.status().get("running"):
                if url:
                    await self.retail_single.start(url=url)
                else:
                    await self.retail_single.start()
        else:
            raise ValueError(f"unknown showcase demo: {target}")

    async def _wait_ready(self, generation: int, target: str) -> None:
        deadline = asyncio.get_running_loop().time() + 45
        last_error: str | None = None
        while asyncio.get_running_loop().time() < deadline:
            self._ensure_current(generation)
            try:
                ready = await self._is_ready(target)
                if ready:
                    return
            except Exception as exc:
                last_error = str(exc)
            await asyncio.sleep(0.5)
        raise TimeoutError(f"{target} readiness timeout" + (f": {last_error}" if last_error else ""))

    async def _is_ready(self, target: str) -> bool:
        if target == "voice":
            return await self._voice_ready()
        if target == "multi":
            return self._multi_ready()
        if target == "single":
            return await self._single_ready()
        return False

    async def _voice_ready(self) -> bool:
        if hasattr(self.sales_voice, "is_ready"):
            return bool(await self.sales_voice.is_ready())
        status = self.sales_voice.status()
        if not status.get("running"):
            return False
        try:
            import websockets

            async with websockets.connect(
                self.sales_voice.upstream_ws,
                open_timeout=1.5,
                ping_interval=None,
                close_timeout=0.5,
            ):
                return True
        except Exception as exc:
            self.sales_voice.last_error = str(exc)
            return False

    def _multi_ready(self) -> bool:
        status = self.live_shelf.status()
        if not status.get("running"):
            return False
        if status.get("ready") is not None:
            if not status.get("ready"):
                return False
            streams = status.get("streams") or []
            if len(streams) >= 4:
                return all(bool(stream.get("ready")) for stream in streams[:4])
            return True
        out_dir = Path(self.live_shelf.out_dir)
        for camera_id in range(1, 5):
            path = out_dir / f"latest_{camera_id}.jpg"
            if not path.is_file():
                return False
            try:
                if path.stat().st_size < 1024:
                    return False
            except OSError:
                return False
        return True

    def _refresh_runtime_health(self) -> None:
        if self.status.state != "RUNNING":
            return
        if self.status.active_demo == "multi":
            adapter_status = self.live_shelf.status()
            if not adapter_status.get("running"):
                self.status.error = "multi background runtime is not ready"
        elif self.status.active_demo == "voice":
            adapter_status = self.sales_voice.status()
            if not adapter_status.get("running"):
                self.status.error = "voice background runtime is not ready"
        elif self.status.active_demo == "single":
            adapter_status = self.retail_single.status()
            if not adapter_status.get("running") and not adapter_status.get("prewarm", {}).get("connected"):
                self.status.error = "single camera prewarm is not ready"

    async def _single_ready(self) -> bool:
        status = self.retail_single.status()
        if "showcase_ready" in status:
            return bool(status["showcase_ready"])
        if not status.get("running") or not status.get("ready"):
            return False
        if not status.get("has_detection_result"):
            return False
        return True

    async def _set_state(self, generation: int, state: str) -> None:
        async with self._lock:
            self._ensure_current_locked(generation)
            self.status.state = state

    async def _complete(self, generation: int, active_demo: str | None, state: str) -> None:
        async with self._lock:
            self._ensure_current_locked(generation)
            self.status.active_demo = active_demo
            self.status.target_demo = active_demo
            self.status.state = state
            self.status.error = None

    async def _fail_if_current(self, generation: int, exc: Exception) -> None:
        async with self._lock:
            if generation != self.status.generation:
                return
            self.status.state = "ERROR"
            self.status.error = str(exc)

    def _ensure_current(self, generation: int) -> None:
        if generation != self.status.generation:
            raise StaleSwitch()

    def _ensure_current_locked(self, generation: int) -> None:
        if generation != self.status.generation:
            raise StaleSwitch()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
