import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.xvf3800_usb_recover import find_xvf3800, recover


class Xvf3800UsbRecoverTests(unittest.TestCase):
    def _device(self, root: Path, name: str = "1-1.3") -> Path:
        device = root / name
        device.mkdir()
        (device / "idVendor").write_text("2886\n", encoding="ascii")
        (device / "idProduct").write_text("001a\n", encoding="ascii")
        (device / "busnum").write_text("1\n", encoding="ascii")
        (device / "devnum").write_text("3\n", encoding="ascii")
        (device / "serial").write_text("test-serial\n", encoding="ascii")
        return device

    def test_finds_exact_xvf3800(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            device = self._device(root)
            found, devnode, serial = find_xvf3800(root)
            self.assertEqual(found, device)
            self.assertEqual(devnode, Path("/dev/bus/usb/001/003"))
            self.assertEqual(serial, "test-serial")

    def test_recovery_is_limited_to_once_per_boot_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sysfs = root / "sysfs"
            sysfs.mkdir()
            self._device(sysfs)
            request = root / "request.json"
            result = root / "result.json"
            marker = root / "run" / "attempt.json"

            def write_request(request_id):
                request.write_text(
                    json.dumps({
                        "request_id": request_id,
                        "requested_at": time.time(),
                        "vendor_id": "2886",
                        "product_id": "001a",
                    }),
                    encoding="ascii",
                )

            write_request("first")
            with patch(
                "tools.xvf3800_usb_recover.wait_for_capture_exit", return_value=True
            ), patch("tools.xvf3800_usb_recover.reset_device") as reset_device:
                self.assertEqual(recover(request, result, marker, sysfs), 0)
                reset_device.assert_called_once_with(Path("/dev/bus/usb/001/003"))

            write_request("second")
            self.assertEqual(recover(request, result, marker, sysfs), 1)
            value = json.loads(result.read_text(encoding="ascii"))
            self.assertFalse(value["ok"])
            self.assertIn("already attempted", value["error"])

    def test_running_capture_does_not_consume_recovery_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sysfs = root / "sysfs"
            sysfs.mkdir()
            self._device(sysfs)
            request = root / "request.json"
            result = root / "result.json"
            marker = root / "run" / "attempt.json"
            request.write_text(
                json.dumps({
                    "request_id": "busy",
                    "requested_at": time.time(),
                    "vendor_id": "2886",
                    "product_id": "001a",
                }),
                encoding="ascii",
            )

            with patch(
                "tools.xvf3800_usb_recover.wait_for_capture_exit", return_value=False
            ), patch("tools.xvf3800_usb_recover.reset_device") as reset_device:
                self.assertEqual(recover(request, result, marker, sysfs), 1)

            self.assertFalse(marker.exists())
            reset_device.assert_not_called()


if __name__ == "__main__":
    unittest.main()
