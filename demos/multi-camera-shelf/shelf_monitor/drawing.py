"""Rendering of detection boxes, ROIs, the inventory panel and alert overlays.

Production-style compact rendering: thin boxes with a tiny confidence label,
one compact color-coded inventory panel, and single-line alert banners. No
``cv2.imshow`` is used so the pipeline runs headless over SSH.
"""

from __future__ import annotations

from typing import Sequence

import cv2
import numpy as np

from .inventory import InventoryState, StockStatus
from .regions import RegionConfig
from .temporal_filter import EventKind, InventoryEvent

# BGR colors
COLOR_BOX = (0, 255, 0)
COLOR_ROI = (255, 200, 0)
COLOR_PANEL = (0, 0, 0)
COLOR_TEXT = (255, 255, 255)
COLOR_REMOVED = (0, 0, 255)      # red
COLOR_RESTOCKED = (0, 200, 255)  # orange
COLOR_PICKED = (0, 255, 255)     # yellow
COLOR_LOW = (0, 102, 255)        # orange
COLOR_OK = (0, 200, 0)           # green
COLOR_HUD = (200, 200, 200)
FONT = cv2.FONT_HERSHEY_SIMPLEX


def _put_text(img: np.ndarray, text: str, org: tuple[int, int],
              scale: float, color, thickness: int = 1, bg: bool = True) -> None:
    """Draw text with an optional dark background for readability."""
    (tw, th), bl = cv2.getTextSize(text, FONT, scale, thickness)
    x, y = org
    if bg:
        cv2.rectangle(img, (x - 2, y - th - 2), (x + tw + 2, y + bl + 2), COLOR_PANEL, -1)
    cv2.putText(img, text, (x, y), FONT, scale, color, thickness, cv2.LINE_AA)


def draw_boxes(img: np.ndarray, boxes: Sequence[Sequence[float]],
               confs: Sequence[float], color=COLOR_BOX, show_conf: bool = True,
               labels: Sequence[str] | None = None) -> None:
    """Draw thin product boxes with a tiny confidence label at the top-left."""
    for idx, (box, conf) in enumerate(zip(boxes, confs)):
        x1, y1, x2, y2 = (int(round(v)) for v in box)
        cv2.rectangle(img, (x1, y1), (x2, y2), color, 1)
        type_label = labels[idx] if labels is not None and idx < len(labels) else ""
        if show_conf or type_label:
            label = " ".join(part for part in (type_label, f"{conf:.2f}" if show_conf else "") if part)
            scale = 0.35
            (tw, th), _ = cv2.getTextSize(label, FONT, scale, 1)
            available = max(10, x2 - x1 - 4)
            if tw > available:
                scale = max(0.20, scale * available / tw)
                (tw, th), _ = cv2.getTextSize(label, FONT, scale, 1)
            # Put the label inside the box so labels on dense neighboring
            # products do not overwrite each other.
            bottom = min(y2, y1 + th + 5)
            cv2.rectangle(img, (x1, y1), (min(x2, x1 + tw + 4), bottom), color, -1)
            cv2.putText(img, label, (x1 + 2, min(y2 - 2, y1 + th + 2)),
                        FONT, scale, COLOR_PANEL, 1, cv2.LINE_AA)


def draw_regions(img: np.ndarray, config: RegionConfig, alpha: float = 0.12) -> None:
    """Draw every ROI rectangle and its product name (small)."""
    overlay = img.copy()
    for reg in config.regions:
        x1, y1, x2, y2 = reg.pixel_roi
        cv2.rectangle(overlay, (x1, y1), (x2, y2), COLOR_ROI, 1)
    cv2.addWeighted(overlay, alpha, img, 1 - alpha, 0, img)
    for reg in config.regions:
        x1, y1, _, _ = reg.pixel_roi
        _put_text(img, reg.name, (x1 + 2, y1 + 12), 0.32, COLOR_ROI, 1, bg=True)


def _status_color(status: StockStatus):
    if status is StockStatus.OUT_OF_STOCK:
        return COLOR_REMOVED
    if status is StockStatus.LOW_STOCK:
        return COLOR_LOW
    return COLOR_OK


def draw_inventory_panel(img: np.ndarray, inv: InventoryState,
                         fps: float, timestamp: float,
                         is_occluded: bool = False) -> None:
    """Compact color-coded inventory panel in the top-left.

    One line per region: ``name: count / capacity`` colored by stock status.
    Total + HUD at the bottom. Low/out rows are colored so no separate
    per-region alert block is needed.
    """
    h, w = img.shape[:2]
    scale = 0.36
    line_h = 15
    pad = 6
    snap = inv.snapshot()
    n_lines = 2 + len(snap) + 2  # title + blank + rows + total + hud
    panel_w = min(w - 2 * pad, 235)
    panel_h = n_lines * line_h + pad * 2

    x0, y0 = pad, pad
    overlay = img.copy()
    cv2.rectangle(overlay, (x0, y0), (x0 + panel_w, y0 + panel_h), COLOR_PANEL, -1)
    cv2.addWeighted(overlay, 0.5, img, 0.5, 0, img)
    cv2.rectangle(img, (x0, y0), (x0 + panel_w, y0 + panel_h), COLOR_ROI, 1)

    y = y0 + pad + line_h
    _put_text(img, "SHELF INVENTORY", (x0 + pad, y), scale + 0.04, COLOR_TEXT, 1, bg=False)
    y += line_h + 2
    for s in snap:
        color = _status_color(s.status)
        line = f"{s.name}: {s.count}/{s.capacity}"
        _put_text(img, line, (x0 + pad, y), scale, color, 1, bg=False)
        y += line_h
    y += 2
    _put_text(img, f"Total Products: {inv.total()}", (x0 + pad, y), scale, COLOR_TEXT, 1, bg=False)
    y += line_h
    hud = f"FPS {fps:.1f}  t={timestamp:.1f}s"
    if is_occluded:
        hud += "  [OCCL]"
    _put_text(img, hud, (x0 + pad, y), scale, COLOR_HUD, 1, bg=False)


def draw_events(img: np.ndarray, events: Sequence[InventoryEvent]) -> None:
    """Draw compact one-line ITEM REMOVED / ITEM RESTOCKED banners at the bottom.

    Capped to the single most recent event so the log never stacks.
    """
    if not events:
        return
    h, w = img.shape[:2]
    scale = 0.65
    line_h = 30
    pad = 8
    banners = []
    for ev in list(events)[-1:]:
        if ev.kind is EventKind.REMOVED:
            color = COLOR_REMOVED
            sign = "-"
        elif ev.kind is EventKind.RESTOCKED:
            color = COLOR_RESTOCKED
            sign = "+"
        else:  # PICKED_UP
            color = COLOR_PICKED
            sign = ""
        banners.append((f"{ev.kind.value}: {ev.name} {sign}{ev.delta}".strip(), color))
    block_h = len(banners) * (line_h + pad) + pad
    # center the banner in the middle of the frame so the demo effect is obvious
    y0 = h // 2 - (len(banners) * (line_h + pad)) // 2
    for text, color in banners:
        (tw, th), _ = cv2.getTextSize(text, FONT, scale, 2)
        bw = tw + pad * 4
        x0 = (w - bw) // 2
        overlay = img.copy()
        cv2.rectangle(overlay, (x0, y0), (x0 + bw, y0 + line_h), COLOR_PANEL, -1)
        cv2.addWeighted(overlay, 0.65, img, 0.35, 0, img)
        cv2.rectangle(img, (x0, y0), (x0 + bw, y0 + line_h), color, 2)
        cv2.putText(img, text, (x0 + (bw - tw) // 2, y0 + line_h - 6),
                    FONT, scale, color, 2, cv2.LINE_AA)
        y0 += line_h + pad


def draw_low_stock(img: np.ndarray, inv: InventoryState) -> None:
    """One compact banner listing regions that need restocking (top-right)."""
    low = inv.low_stock_regions()
    if not low:
        return
    h, w = img.shape[:2]
    scale = 0.4
    line_h = 16
    pad = 6
    names = [s.name for s in low]
    # wrap into a compact block: title + 1-2 lines of names
    title = "RESTOCK REQUIRED"
    line1 = ", ".join(names[:6])
    line2 = ", ".join(names[6:]) if len(names) > 6 else ""
    rows = [title, line1] + ([line2] if line2 else [])
    panel_w = min(w - 2 * pad, 300)
    panel_h = len(rows) * line_h + pad * 2
    x0 = w - panel_w - pad
    y0 = pad
    overlay = img.copy()
    cv2.rectangle(overlay, (x0, y0), (x0 + panel_w, y0 + panel_h), COLOR_PANEL, -1)
    cv2.addWeighted(overlay, 0.55, img, 0.45, 0, img)
    cv2.rectangle(img, (x0, y0), (x0 + panel_w, y0 + panel_h), COLOR_LOW, 1)
    y = y0 + pad + line_h
    cv2.putText(img, title, (x0 + pad, y), FONT, scale, COLOR_LOW, 1, cv2.LINE_AA)
    y += line_h
    cv2.putText(img, line1, (x0 + pad, y), FONT, scale, COLOR_TEXT, 1, cv2.LINE_AA)
    if line2:
        y += line_h
        cv2.putText(img, line2, (x0 + pad, y), FONT, scale, COLOR_TEXT, 1, cv2.LINE_AA)


def draw_roi_inspection(img: np.ndarray, config: RegionConfig) -> None:
    """Standalone ROI visualization: draw every ROI with id + name + capacity."""
    draw_regions(img, config, alpha=0.25)
    for reg in config.regions:
        x1, y1, x2, y2 = reg.pixel_roi
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        info = f"{reg.id} | {reg.name} | cap={reg.capacity}"
        _put_text(img, info, (x1 + 2, y2 - 6), 0.4, COLOR_TEXT, 1, bg=True)
        cv2.drawMarker(img, (cx, cy), COLOR_ROI, cv2.MARKER_CROSS, 12, 1)
