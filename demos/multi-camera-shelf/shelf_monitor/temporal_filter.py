"""Multi-frame count stabilization and pick / restock event confirmation.

A single frame of YOLO output is too noisy to drive inventory decisions:
boxes flicker, the customer's arm occludes product, and occasional misses
happen. This module stabilizes per-region counts over a sliding window and
only confirms a change after it has persisted long enough to be real.

Event debounce (persist): a confirmed count change does NOT emit an event
immediately. The new count must stay stable for ``event_persist_seconds``;
if it reverts to the previously-emitted value within that window (typical
hand-occlusion flicker 1->0->1), no event is emitted at all. A real pickup
(1->0 and stays) emits exactly one ITEM REMOVED after the persist window.

Shared by the PC pipeline and the RK3576 pipeline.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from statistics import median
from typing import Iterable

from .regions import RegionConfig

logger = logging.getLogger("shelf_monitor.temporal_filter")


class EventKind(str, Enum):
    REMOVED = "ITEM REMOVED"
    RESTOCKED = "ITEM RESTOCKED"
    PICKED_UP = "ITEM PICKED UP"


@dataclass
class InventoryEvent:
    """A confirmed (persisted) change in a region's stable count."""

    region_id: str
    name: str
    kind: EventKind
    delta: int  # always positive; sign is implied by kind
    timestamp: float


@dataclass
class StabilizerResult:
    """Output of one :meth:`TemporalStabilizer.update` call."""

    confirmed_counts: dict[str, int]
    events: list[InventoryEvent] = field(default_factory=list)
    is_occluded: bool = False
    phase: str = "normal"  # "baseline" while initializing, "normal" afterwards
    raw_counts: dict[str, int] = field(default_factory=dict)


class TemporalStabilizer:
    """Stabilize per-region product counts and emit pick / restock events.

    Algorithm (per region):

    1. Keep the last ``history_size`` raw counts.
    2. Candidate stable count = ``round(median(history))``.
    3. The candidate must persist for ``confirmation_frames`` consecutive
       frames before it is promoted to the confirmed count.
    4. A single miss never zeroes a region.
    5. If the *total* raw count drops by more than ``occlusion_drop_ratio``
       from the last stable total, the stabilizer enters an occlusion state
       and freezes confirmation. Recovery within ``occlusion_recovery_frames``
       discards the dip; otherwise the lower count confirms normally.
    6. Confirmed counts are clamped to ``[0, capacity]``.
    7. Event debounce: a confirmed change emits an event only after the new
       count stays stable for ``event_persist_seconds``. A revert to the
       previously-emitted value within that window cancels the event
       (suppresses hand-occlusion flicker).
    """

    def __init__(
        self,
        region_config: RegionConfig,
        history_size: int = 15,
        confirmation_frames: int = 10,
        occlusion_recovery_frames: int = 15,
        occlusion_drop_ratio: float = 0.30,
        event_duration: float = 2.0,
        event_persist_seconds: float = 2.0,
        allow_over_capacity: bool = False,
    ) -> None:
        self.region_config = region_config
        self.history_size = history_size
        self.confirmation_frames = confirmation_frames
        self.occlusion_recovery_frames = occlusion_recovery_frames
        self.occlusion_drop_ratio = occlusion_drop_ratio
        self.event_duration = event_duration
        self.event_persist_seconds = event_persist_seconds
        self.allow_over_capacity = allow_over_capacity

        self._ids = [r.id for r in region_config.regions]
        self._capacity = {r.id: r.capacity for r in region_config.regions}
        self._names = {r.id: r.name for r in region_config.regions}

        # per-region state
        self._history: dict[str, deque] = {rid: deque(maxlen=history_size) for rid in self._ids}
        self._candidate: dict[str, int] = {rid: 0 for rid in self._ids}
        self._streak: dict[str, int] = {rid: 0 for rid in self._ids}
        self._confirmed: dict[str, int] = {rid: 0 for rid in self._ids}
        self._previous_stable: dict[str, int] = {rid: 0 for rid in self._ids}

        # event debounce state
        self._last_emitted: dict[str, int] = {rid: 0 for rid in self._ids}
        self._pending_new: dict[str, int | None] = {rid: None for rid in self._ids}
        self._pending_since: dict[str, float] = {rid: 0.0 for rid in self._ids}

        # occlusion state
        self._stable_total: int = 0
        self._occluded: bool = False
        self._occlusion_counter: int = 0

        # baseline (auto) state
        self._baseline_mode = region_config.baseline_mode
        self._baseline_seconds = region_config.baseline_seconds
        self._baseline_raw: dict[str, list[int]] = {rid: [] for rid in self._ids}
        self._baseline_done = False
        if self._baseline_mode == "config":
            for rid in self._ids:
                self._confirmed[rid] = self._capacity[rid]
                self._previous_stable[rid] = self._capacity[rid]
                self._last_emitted[rid] = self._capacity[rid]
            self._stable_total = sum(self._confirmed.values())
            self._baseline_done = True

    # ------------------------------------------------------------------ #
    @property
    def confirmed_counts(self) -> dict[str, int]:
        return dict(self._confirmed)

    def _clamp(self, rid: str, value: int) -> int:
        cap = self._capacity[rid]
        if value < 0:
            return 0
        if value > cap and not self.allow_over_capacity:
            return cap
        return value

    def _finalize_baseline(self) -> None:
        """Compute initial confirmed counts from the baseline window (auto mode)."""
        for rid in self._ids:
            raws = self._baseline_raw[rid]
            init_val = int(round(median(raws))) if raws else self._capacity[rid]
            init_val = self._clamp(rid, init_val)
            self._confirmed[rid] = init_val
            self._previous_stable[rid] = init_val
            self._last_emitted[rid] = init_val
            self._history[rid].extend(raws[-self.history_size:] or [init_val])
            self._candidate[rid] = init_val
            self._streak[rid] = self.confirmation_frames
        self._stable_total = sum(self._confirmed.values())
        self._baseline_done = True
        logger.info("auto baseline initialized: %s", self._confirmed)

    # ------------------------------------------------------------------ #
    def update(self, raw_counts: dict[str, int], timestamp: float) -> StabilizerResult:
        """Feed one frame's per-region raw counts and return the stabilized view."""
        counts = {rid: int(raw_counts.get(rid, 0)) for rid in self._ids}

        # ---- baseline window (auto mode) ----
        if not self._baseline_done:
            for rid in self._ids:
                self._baseline_raw[rid].append(counts[rid])
            if timestamp >= self._baseline_seconds:
                self._finalize_baseline()
                return StabilizerResult(
                    confirmed_counts=dict(self._confirmed),
                    events=[],
                    is_occluded=False,
                    phase="normal",
                    raw_counts=counts,
                )
            return StabilizerResult(
                confirmed_counts=dict(self._confirmed),
                events=[],
                is_occluded=False,
                phase="baseline",
                raw_counts=counts,
            )

        # ---- normal mode ----
        for rid in self._ids:
            self._history[rid].append(counts[rid])

        for rid in self._ids:
            hist = self._history[rid]
            cand = int(round(median(hist))) if hist else 0
            cand = self._clamp(rid, cand)
            if cand == self._candidate[rid]:
                self._streak[rid] += 1
            else:
                self._candidate[rid] = cand
                self._streak[rid] = 1

        # occlusion detection on total
        raw_total = sum(counts.values())
        threshold_total = self._stable_total * (1.0 - self.occlusion_drop_ratio)
        if self._stable_total > 0 and raw_total < threshold_total:
            self._occluded = True
            self._occlusion_counter = 0

        events: list[InventoryEvent] = []

        if self._occluded:
            self._occlusion_counter += 1
            recovered = raw_total >= threshold_total
            timed_out = self._occlusion_counter > self.occlusion_recovery_frames
            if recovered or timed_out:
                self._occluded = False
                if recovered:
                    for rid in self._ids:
                        self._candidate[rid] = self._confirmed[rid]
                        self._streak[rid] = 0
                    logger.debug("occlusion recovered within %d frames", self._occlusion_counter)
        else:
            # promote candidates that have persisted long enough
            for rid in self._ids:
                if self._streak[rid] >= self.confirmation_frames:
                    cand = self._candidate[rid]
                    if cand != self._confirmed[rid]:
                        prev = self._confirmed[rid]
                        self._confirmed[rid] = cand
                        self._previous_stable[rid] = prev

        # ---- event debounce (persist) ----
        # A confirmed value different from last_emitted starts/restarts a
        # pending timer. If it reverts to last_emitted, the pending is
        # cancelled (occlusion flicker). If it persists for
        # event_persist_seconds, the event is emitted.
        for rid in self._ids:
            conf = self._confirmed[rid]
            if conf == self._last_emitted[rid]:
                self._pending_new[rid] = None
            else:
                if self._pending_new[rid] != conf:
                    self._pending_new[rid] = conf
                    self._pending_since[rid] = timestamp
        for rid in self._ids:
            pn = self._pending_new[rid]
            if pn is not None and (timestamp - self._pending_since[rid]) >= self.event_persist_seconds:
                prev = self._last_emitted[rid]
                removed = prev - pn
                added = pn - prev
                if removed > 0:
                    events.append(InventoryEvent(rid, self._names[rid], EventKind.REMOVED, removed, timestamp))
                elif added > 0:
                    events.append(InventoryEvent(rid, self._names[rid], EventKind.RESTOCKED, added, timestamp))
                self._last_emitted[rid] = pn
                self._pending_new[rid] = None

        self._stable_total = sum(self._confirmed.values())
        return StabilizerResult(
            confirmed_counts=dict(self._confirmed),
            events=events,
            is_occluded=self._occluded,
            phase="normal",
            raw_counts=counts,
        )

    def recent_events(self, events: Iterable[InventoryEvent], now: float) -> list[InventoryEvent]:
        """Return events whose timestamp is within ``event_duration`` of *now*."""
        return [e for e in events if (now - e.timestamp) <= self.event_duration]
