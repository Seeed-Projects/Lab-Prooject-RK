import unittest
from dataclasses import dataclass
from enum import Enum

from tools.shelf_business_events import ShelfEventTracker


class Kind(str, Enum):
    REMOVED = "ITEM REMOVED"
    RESTOCKED = "ITEM RESTOCKED"


class Status(str, Enum):
    IN_STOCK = "IN STOCK"
    LOW_STOCK = "LOW STOCK"
    OUT_OF_STOCK = "OUT OF STOCK"


@dataclass
class Event:
    region_id: str
    kind: Kind
    delta: int


@dataclass
class Row:
    id: str
    name: str
    count: int
    status: Status


class Inventory:
    def __init__(self, row):
        self.row = row

    def snapshot(self):
        return [self.row]


class ShelfBusinessEventTests(unittest.TestCase):
    def test_real_removed_event_creates_alert_and_shared_timeline(self):
        tracker = ShelfEventTracker(camera_id=2)
        tracker.ingest(
            [Event("milk", Kind.REMOVED, 3)],
            Inventory(Row("milk", "Milk", 0, Status.OUT_OF_STOCK)),
        )

        alerts, timeline = tracker.snapshot()
        self.assertEqual(alerts[0]["camera"], 2)
        self.assertEqual(alerts[0]["previous_count"], 3)
        self.assertEqual(alerts[0]["current_count"], 0)
        self.assertEqual(alerts[0]["source"], "detection")
        self.assertTrue(any(item.get("type") == "stock_alert" for item in timeline))

    def test_real_restock_event_clears_active_alert(self):
        tracker = ShelfEventTracker(camera_id=4)
        tracker.ingest(
            [Event("milk", Kind.REMOVED, 3)],
            Inventory(Row("milk", "Milk", 0, Status.OUT_OF_STOCK)),
        )
        tracker.ingest(
            [Event("milk", Kind.RESTOCKED, 3)],
            Inventory(Row("milk", "Milk", 3, Status.IN_STOCK)),
        )

        alerts, timeline = tracker.snapshot()
        self.assertEqual(alerts, [])
        self.assertTrue(any("recovered" in item["title"] for item in timeline))

    def test_real_single_item_decrease_creates_low_stock_alert(self):
        tracker = ShelfEventTracker(camera_id=3)
        tracker.ingest(
            [Event("band_c", Kind.REMOVED, 1)],
            Inventory(Row("band_c", "Lower Mid", 9, Status.LOW_STOCK)),
        )

        alerts, _ = tracker.snapshot()
        self.assertEqual(alerts[0]["camera"], 3)
        self.assertEqual(alerts[0]["previous_count"], 10)
        self.assertEqual(alerts[0]["current_count"], 9)
        self.assertEqual(alerts[0]["status"], "LOW_STOCK")
        self.assertEqual(alerts[0]["source"], "detection")


if __name__ == "__main__":
    unittest.main()
