#!/usr/bin/env python3
"""Collect RK3588 OS, kernel, RKNPU, RKNNLite and OpenCV versions."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def command(*args: str) -> str | None:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=5, check=False)
        text = (result.stdout or result.stderr).strip()
        return text or None
    except (FileNotFoundError, subprocess.SubprocessError):
        return None


def read(path: str) -> str | None:
    try:
        return Path(path).read_text(errors="replace").strip() or None
    except OSError:
        return None


def find_files(names: list[str], roots: list[str]) -> list[dict]:
    found = []
    for root in roots:
        base = Path(root)
        if not base.exists():
            continue
        for name in names:
            for path in base.rglob(name):
                try:
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    found.append({"path": str(path), "size": path.stat().st_size, "sha256": digest})
                except OSError:
                    pass
    return found


def module_version(module_name: str) -> dict:
    try:
        module = __import__(module_name)
        return {"available": True, "version": getattr(module, "__version__", "unknown"), "file": getattr(module, "__file__", None)}
    except Exception as exc:
        return {"available": False, "error": repr(exc)}


def main() -> int:
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "platform": {
            "uname": platform.uname()._asdict(),
            "machine": platform.machine(),
            "python": sys.version,
            "executable": sys.executable,
            "os_release": read("/etc/os-release"),
            "device_tree_compatible": read("/proc/device-tree/compatible"),
            "device_tree_model": read("/proc/device-tree/model"),
        },
        "kernel": {
            "version": platform.release(),
            "uname_a": command("uname", "-a"),
            "rknpu_module": command("modinfo", "rknpu"),
            "rknpu_debug_version": read("/sys/kernel/debug/rknpu/version"),
            "rknpu_module_version": read("/sys/module/rknpu/version"),
            "rknpu_devices": sorted(str(path) for path in Path("/dev").glob("*rknpu*")),
        },
        "packages": {
            "rknnlite": module_version("rknnlite"),
            "cv2": module_version("cv2"),
            "numpy": module_version("numpy"),
        },
        "libraries": {
            "ldconfig_rknn": command("sh", "-c", "ldconfig -p 2>/dev/null | grep -i rknn"),
            "rknn_runtime_files": find_files(
                ["librknnrt.so", "librknn_api.so"],
                ["/usr/lib", "/usr/local/lib", "/lib", "/opt"],
            ),
        },
        "video": {
            "ffmpeg": command("ffmpeg", "-version"),
            "v4l2_devices": sorted(str(path) for path in Path("/dev").glob("video*")),
        },
        "environment": {
            key: os.environ.get(key)
            for key in ("LD_LIBRARY_PATH", "PYTHONPATH", "RKNN_LOG_LEVEL")
        },
    }
    output = ROOT / "diagnostics" / "rk3588_probe.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nSaved: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

