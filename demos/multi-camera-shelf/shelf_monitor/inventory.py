"""Inventory state: per-region stock status and pick / restock deltas.

This module turns *confirmed* (stabilized) counts into the information shown
on the output panel: how many of each product are on the shelf, whether each
is IN / LOW / OUT OF STOCK, the total, and the most recent pick or restock
delta per region.

It depends only on :class:`shelf_monitor.regions.RegionConfig` and the
confirmed counts produced by :class:`shelf_monitor.temporal_filter.TemporalStabilizer`,
so it is reused unchanged by the PC and RK3576 pipelines.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .regions import RegionConfig
from .temporal_filter import EventKind, InventoryEvent


class StockStatus(str, Enum):
    """Human-readable stock state for one region."""

    IN_STOCK = "IN STOCK"
    LOW_STOCK = "LOW STOCK"
    OUT_OF_STOCK = "OUT OF STOCK"


def stock_status(count: int, low_stock_threshold: int) -> StockStatus:
    """Classify a confirmed count into a stock status.

    * ``count == 0``                 -> OUT OF STOCK
    * ``0 < count <= threshold``    -> LOW STOCK
    * ``count > threshold``         -> IN STOCK
    """
    if count <= 0:
        return StockStatus.OUT_OF_STOCK
    if count <= low_stock_threshold:
        return StockStatus.LOW_STOCK
    return StockStatus.IN_STOCK


def compute_removed(previous: int, confirmed: int) -> int:
    """Return how many products were removed (>= 0)."""
    return max(0, previous - confirmed)


def compute_added(previous: int, confirmed: int) -> int:
    """Return how many products were restocked (>= 0)."""
    return max(0, confirmed - previous)


@dataclass
class RegionSnapshot:
    """One row of the inventory panel."""

    id: str
    name: str
    count: int
    capacity: int
    low_stock_threshold: int
    status: StockStatus
    last_delta: int = 0  # signed: negative = removed, positive = restocked
    last_event_kind: EventKind | None = None


@dataclass
class InventoryState:
    """Current inventory view derived from confirmed counts + events."""

    region_config: RegionConfig
    confirmed_counts: dict[str, int] = field(default_factory=dict)
    _latest_event: dict[str, InventoryEvent] = field(default_factory=dict)

    def update(self, confirmed_counts: dict[str, int], events: list[InventoryEvent]) -> None:
        """Store the latest confirmed counts and the most recent event per region."""
        self.confirmed_counts = {rid: int(confirmed_counts.get(rid, 0)) for rid in self._ids()}
        for ev in events:
            self._latest_event[ev.region_id] = ev

    def _ids(self) -> list[str]:
        return [r.id for r in self.region_config.regions]

    def count(self, region_id: str) -> int:
        return int(self.confirmed_counts.get(region_id, 0))

    def total(self) -> int:
        """Total confirmed products across all regions."""
        return sum(self.count(r.id) for r in self.region_config.regions)

    def status(self, region_id: str) -> StockStatus:
        reg = self.region_config.get(region_id)
        if reg is None:
            return StockStatus.OUT_OF_STOCK
        return stock_status(self.count(region_id), reg.low_stock_threshold)

    def snapshot(self) -> list[RegionSnapshot]:
        """Build the full panel: one :class:`RegionSnapshot` per region."""
        out: list[RegionSnapshot] = []
        for reg in self.region_config.regions:
            cnt = self.count(reg.id)
            ev = self._latest_event.get(reg.id)
            last_delta = 0
            kind = None
            if ev is not None:
                kind = ev.kind
                last_delta = -ev.delta if ev.kind is EventKind.REMOVED else ev.delta
            out.append(
                RegionSnapshot(
                    id=reg.id,
                    name=reg.name,
                    count=cnt,
                    capacity=reg.capacity,
                    low_stock_threshold=reg.low_stock_threshold,
                    status=stock_status(cnt, reg.low_stock_threshold),
                    last_delta=last_delta,
                    last_event_kind=kind,
                )
            )
        return out

    def low_stock_regions(self) -> list[RegionSnapshot]:
        """Regions that are LOW STOCK or OUT OF STOCK (need restocking)."""
        return [s for s in self.snapshot() if s.status is not StockStatus.IN_STOCK]
