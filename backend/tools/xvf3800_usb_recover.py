#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import time
import uuid
from pathlib import Path
from typing import Any


EXPECTED_VENDOR = "2886"
EXPECTED_PRODUCT = "001a"
USBDEVFS_RESET = (ord("U") << 8) | 20


def _read_text(path: Path) -> str:
    return path.read_text(encoding="ascii").strip().lower()


def find_xvf3800(sysfs_root: Path) -> tuple[Path, Path, str | None]:
    matches: list[tuple[Path, Path, str | None]] = []
    for device in sysfs_root.iterdir():
        try:
            if (
                _read_text(device / "idVendor") != EXPECTED_VENDOR
                or _read_text(device / "idProduct") != EXPECTED_PRODUCT
            ):
                continue
            bus = int(_read_text(device / "busnum"))
            number = int(_read_text(device / "devnum"))
            serial_path = device / "serial"
            serial = _read_text(serial_path) if serial_path.exists() else None
        except (FileNotFoundError, NotADirectoryError, OSError, ValueError):
            continue
        matches.append(
            (device, Path(f"/dev/bus/usb/{bus:03d}/{number:03d}"), serial)
        )
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one XVF3800, found {len(matches)}")
    return matches[0]


def capture_is_running(proc_root: Path = Path("/proc")) -> bool:
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if b"arecord" in command and b"hw:Array,0" in command:
            return True
    return False


def wait_for_capture_exit(timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not capture_is_running():
            return True
        time.sleep(0.1)
    return not capture_is_running()


def reset_device(devnode: Path) -> None:
    descriptor = os.open(
        devnode, os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    )
    try:
        fcntl.ioctl(descriptor, USBDEVFS_RESET, 0)
    finally:
        os.close(descriptor)


def _read_request(path: Path) -> dict[str, Any]:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
            raise RuntimeError("Recovery request must be a small regular file")
        payload = os.read(descriptor, 4097)
    finally:
        os.close(descriptor)
    request = json.loads(payload)
    if not isinstance(request, dict):
        raise RuntimeError("Recovery request is not a JSON object")
    if request.get("vendor_id") != EXPECTED_VENDOR or request.get("product_id") != EXPECTED_PRODUCT:
        raise RuntimeError("Recovery request does not target the XVF3800")
    if not isinstance(request.get("request_id"), str) or len(request["request_id"]) > 64:
        raise RuntimeError("Recovery request ID is invalid")
    requested_at = request.get("requested_at")
    if not isinstance(requested_at, (int, float)) or abs(time.time() - requested_at) > 60:
        raise RuntimeError("Recovery request is stale")
    return request


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o644,
    )
    try:
        os.write(descriptor, (json.dumps(value, ensure_ascii=True) + "\n").encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


def recover(request_path: Path, result_path: Path, marker_path: Path, sysfs_root: Path) -> int:
    request_id: str | None = None
    result: dict[str, Any]
    try:
        request = _read_request(request_path)
        request_id = request["request_id"]
        if marker_path.exists():
            raise RuntimeError("XVF3800 recovery was already attempted during this boot")
        if not wait_for_capture_exit():
            raise RuntimeError("Refusing USB reset while the owned arecord capture is running")

        device, devnode, serial = find_xvf3800(sysfs_root)
        # Consume the per-boot attempt only after all safety checks pass and
        # immediately before issuing the device-scoped ioctl.
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(
            marker_path,
            {"request_id": request_id, "attempted_at": time.time()},
        )
        reset_device(devnode)
        result = {
            "request_id": request_id,
            "ok": True,
            "method": "USBDEVFS_RESET",
            "sysfs_device": device.name,
            "device_node": str(devnode),
            "serial": serial,
            "completed_at": time.time(),
        }
    except Exception as exc:
        result = {
            "request_id": request_id,
            "ok": False,
            "method": "USBDEVFS_RESET",
            "error": str(exc),
            "completed_at": time.time(),
        }
    finally:
        try:
            request_path.unlink()
        except FileNotFoundError:
            pass
    _write_json(result_path, result)
    return 0 if result["ok"] else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--marker", type=Path, required=True)
    parser.add_argument("--sysfs-root", type=Path, default=Path("/sys/bus/usb/devices"))
    args = parser.parse_args()
    return recover(args.request, args.result, args.marker, args.sysfs_root)


if __name__ == "__main__":
    raise SystemExit(main())
