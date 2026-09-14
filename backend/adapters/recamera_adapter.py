from __future__ import annotations

import asyncio
import json
import subprocess
from typing import Any


class ReCameraAdapter:
    def __init__(self, device_ip: str = "192.168.42.1", ws_port: int = 9002, rtsp_port: int = 8554):
        self.device_ip = device_ip
        self.ws_port = ws_port
        self.rtsp_port = rtsp_port
        self.upstream_ws = f"ws://{device_ip}:{ws_port}"
        self.detected_rtsp = f"rtsp://{device_ip}:{rtsp_port}/detected"
        self.original_rtsp = f"rtsp://{device_ip}:{rtsp_port}/original"
        self.latest: dict[str, Any] | None = None
        self.last_error: str | None = None

    async def status(self) -> dict[str, Any]:
        network_reachable, ws_port_open, rtsp_port_open = await asyncio.gather(
            self._network_reachable_async(),
            self._port_open_async(self.ws_port),
            self._port_open_async(self.rtsp_port),
        )
        network_connected = network_reachable or ws_port_open or rtsp_port_open
        return {
            "device_ip": self.device_ip,
            "upstream_ws": self.upstream_ws,
            "detected_rtsp": self.detected_rtsp,
            "original_rtsp": self.original_rtsp,
            "network_connected": network_connected,
            "ws_port_open": ws_port_open,
            "rtsp_port_open": rtsp_port_open,
            "latest": self.latest,
            "last_error": self.last_error,
        }

    async def hardware_status(self, *, stream_ready: bool = False) -> dict[str, Any]:
        network_reachable, ws_port_open, rtsp_port_open = await asyncio.gather(
            self._network_reachable_async(),
            self._port_open_async(self.ws_port),
            self._port_open_async(self.rtsp_port),
        )
        network_connected = network_reachable or ws_port_open or rtsp_port_open
        connected = bool(stream_ready)
        state = "Connected" if connected else "Connecting" if network_connected else "Disconnected"
        return {
            "name": "reCamera",
            "connected": connected,
            "state": state,
            "detected_by": f"{self.device_ip} network / RTSP / detected frame",
            "details": {
                "device_ip": self.device_ip,
                "network_connected": network_connected,
                "stream_ready": bool(stream_ready),
                "ws_port_open": ws_port_open,
                "rtsp_port_open": rtsp_port_open,
                "upstream_ws": self.upstream_ws,
                "detected_rtsp": self.detected_rtsp,
            },
        }

    async def _network_reachable_async(self) -> bool:
        try:
            process = await asyncio.create_subprocess_exec(
                "ping", "-c", "1", "-W", "1", self.device_ip,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                return await asyncio.wait_for(process.wait(), timeout=2.0) == 0
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return False
        except OSError:
            return False

    async def forward_websocket(self, send_json) -> None:
        try:
            import websockets
        except Exception as exc:
            self.last_error = str(exc)
            await send_json({"type": "connection", "status": "disconnected", "error": str(exc)})
            return

        while True:
            try:
                await send_json({"type": "connection", "status": "reconnecting", "url": self.upstream_ws})
                async with websockets.connect(
                    self.upstream_ws,
                    ping_interval=20,
                    ping_timeout=20,
                    proxy=None,
                ) as websocket:
                    await send_json({"type": "connection", "status": "connected", "url": self.upstream_ws})
                    async for message in websocket:
                        payload = self._decode_json(message)
                        if payload is None:
                            continue
                        self.latest = payload
                        self.last_error = None
                        await send_json({"type": "recamera.status", "data": payload})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                await send_json({"type": "connection", "status": "disconnected", "error": str(exc)})
                await asyncio.sleep(2)

    async def _port_open_async(self, port: int) -> bool:
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.device_ip, port), timeout=1.5
            )
            writer.close()
            await writer.wait_closed()
            return True
        except (OSError, asyncio.TimeoutError):
            return False

    @staticmethod
    def _decode_json(message: str) -> dict[str, Any] | None:
        try:
            value = json.loads(message)
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None
