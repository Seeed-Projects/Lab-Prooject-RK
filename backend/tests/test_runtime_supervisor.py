import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from adapters.sales_voice_adapter import SalesVoiceAdapter
from app.runtime_supervisor import RuntimeState, RuntimeSupervisor


class FakeVoice:
    def __init__(self, *, docker_ready=True, device_ready=True, websocket_ready=True):
        self.docker_ready = docker_ready
        self.device_ready = device_ready
        self.websocket_ready = websocket_ready
        self.running = False
        self.starts = 0
        self.stops = 0
        self.capture_healthy = True
        self.model_health = {"stt_ready": True, "llm_ready": True}
        self.usb_recoveries = 0
        self.usb_recovery_result = {"ok": True, "method": "test"}

    async def probe_docker(self):
        return self.docker_ready, None if self.docker_ready else "docker unavailable"

    async def probe_device(self):
        return self.device_ready, None if self.device_ready else "device unavailable"

    async def probe_websocket(self):
        return self.websocket_ready, None if self.websocket_ready else "8765 unavailable"

    async def probe_model_services(self):
        return self.model_health

    async def start(self, language="zh"):
        self.running = True
        self.starts += 1
        return self.status()

    async def stop_runtime(self):
        self.running = False
        self.stops += 1
        return self.status()

    async def recover_usb_device(self):
        self.usb_recoveries += 1
        return self.usb_recovery_result

    def status(self):
        return {"running": self.running}

    def capture_health(self):
        return {
            "healthy": self.running and self.capture_healthy,
            "gate_required": False,
            "process_alive": self.running,
        }


class FakeSingle:
    source_url = "rtsp://192.168.42.1:8554/detected"

    def __init__(self, *, connected=False):
        self.connected = connected
        self.starts = 0

    async def ensure_prewarm(self, *, url):
        self.starts += 1
        return self.prewarm_status()

    def prewarm_status(self):
        return {
            "running": True,
            "network_reachable": True,
            "rtsp_port_ready": True,
            "connected": self.connected,
            "connection_attempts": self.starts,
            "last_error": None if self.connected else "waiting for first frame",
        }

    def status(self):
        return {"websocket_connected": self.connected}


class FakeMulti:
    def __init__(self):
        self.prepared = 0

    async def prepare(self):
        self.prepared += 1
        return {"prepared": True}


class FakeHardware:
    def __init__(self):
        self.running = False

    async def start(self):
        self.running = True
        return self.status()

    def status(self):
        return {"running": self.running}


class RuntimeSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.voice = FakeVoice()
        self.single = FakeSingle()
        self.multi = FakeMulti()
        self.hardware = FakeHardware()
        self.supervisor = RuntimeSupervisor(
            sales_voice=self.voice,
            retail_single=self.single,
            live_shelf=self.multi,
            hardware_detector=self.hardware,
            backend_port=0,
            frontend_port=0,
            retry_schedule=(0.01, 0.02),
            kernel_watchdog=False,
            capture_poll_interval=0.01,
        )

    async def asyncTearDown(self):
        await self.supervisor.stop()

    async def test_bootstrap_starts_isolated_runtimes_and_only_prepares_multi(self):
        self.supervisor.start()
        await asyncio.sleep(0.05)

        status = self.supervisor.status()["runtimes"]
        self.assertEqual(status["voice"]["state"], RuntimeState.READY.value)
        self.assertEqual(status["recamera"]["phase"], "STREAM_WAITING")
        self.assertEqual(status["multi"]["phase"], "PREPARED_IDLE")
        self.assertEqual(self.voice.starts, 1)
        self.assertEqual(self.multi.prepared, 1)

    async def test_voice_dependency_failure_does_not_block_other_runtimes(self):
        self.voice.docker_ready = False
        self.supervisor.start()
        await asyncio.sleep(0.05)

        status = self.supervisor.status()["runtimes"]
        self.assertEqual(status["voice"]["state"], RuntimeState.WAITING_DEPENDENCY.value)
        self.assertGreaterEqual(self.single.starts, 1)
        self.assertEqual(status["multi"]["phase"], "PREPARED_IDLE")

    async def test_voice_request_wakes_dependency_backoff(self):
        self.voice.docker_ready = False
        self.supervisor.start()
        await asyncio.sleep(0.02)
        self.voice.docker_ready = True
        await self.supervisor.request_voice("en")
        await asyncio.sleep(0.03)

        self.assertEqual(
            self.supervisor.status()["runtimes"]["voice"]["state"],
            RuntimeState.READY.value,
        )

    async def test_voice_waits_for_both_model_services_before_ready(self):
        self.voice.model_health = {"stt_ready": True, "llm_ready": False}
        self.supervisor.start()
        await asyncio.sleep(0.04)

        status = self.supervisor.status()["runtimes"]["voice"]
        self.assertEqual(status["state"], RuntimeState.STARTING.value)
        self.assertEqual(status["phase"], "WAITING_MODEL_SERVICES")
        self.assertFalse(self.voice.running)
        self.assertEqual(self.voice.starts, 0)
        self.assertEqual(self.voice.stops, 0)

        self.voice.model_health = {"stt_ready": True, "llm_ready": True}
        self.supervisor._voice_wake.set()
        await asyncio.sleep(0.04)
        self.assertEqual(
            self.supervisor.status()["runtimes"]["voice"]["state"],
            RuntimeState.READY.value,
        )

    async def test_capture_loss_stops_owned_runtime_and_recovers(self):
        self.supervisor.start()
        await asyncio.sleep(0.03)
        first_starts = self.voice.starts

        self.voice.capture_healthy = False
        self.supervisor._voice_wake.set()
        await asyncio.sleep(0.05)
        self.assertGreaterEqual(self.voice.stops, 1)

        self.voice.capture_healthy = True
        self.supervisor._voice_wake.set()
        await asyncio.sleep(0.04)

        status = self.supervisor.status()["runtimes"]["voice"]
        self.assertGreater(self.voice.starts, first_starts)
        self.assertTrue(self.voice.running)
        self.assertEqual(status["state"], RuntimeState.READY.value)

    async def test_transient_heartbeat_stale_requires_three_checks(self):
        self.supervisor.capture_poll_interval = 0.05
        self.supervisor.start()
        await asyncio.sleep(0.03)
        self.voice.capture_healthy = False

        self.supervisor._voice_wake.set()
        await asyncio.sleep(0.02)
        self.assertEqual(self.voice.stops, 0)
        self.assertEqual(
            self.supervisor.status()["runtimes"]["voice"]["phase"],
            "DEGRADED_CAPTURE",
        )

        await asyncio.sleep(0.04)
        self.assertEqual(self.voice.stops, 0)
        await asyncio.sleep(0.07)
        self.assertGreaterEqual(self.voice.stops, 1)

    async def test_transient_heartbeat_recovers_without_restart(self):
        self.supervisor.capture_poll_interval = 0.05
        self.supervisor.start()
        await asyncio.sleep(0.03)
        self.voice.capture_healthy = False
        self.supervisor._voice_wake.set()
        await asyncio.sleep(0.06)
        self.assertEqual(self.voice.stops, 0)

        self.voice.capture_healthy = True
        self.supervisor._voice_wake.set()
        await asyncio.sleep(0.03)

        status = self.supervisor.status()["runtimes"]["voice"]
        self.assertEqual(self.voice.stops, 0)
        self.assertEqual(status["state"], RuntimeState.READY.value)

    def test_voice_status_path_never_runs_hardware_probe(self):
        voice = SalesVoiceAdapter()
        probe = Mock(side_effect=AssertionError("status must not probe hardware"))
        voice.probe_device = probe
        voice.status()
        probe.assert_not_called()

    def test_capture_health_requires_fresh_nonempty_heartbeat(self):
        with tempfile.TemporaryDirectory() as directory:
            voice = SalesVoiceAdapter(out_dir=Path(directory))
            voice.process = Mock(pid=1234, returncode=None)
            voice.started_at = time.monotonic()
            voice._owned_process_tree = Mock(return_value=[
                {"pid": 1234, "alive": True, "state": "S", "command": "bash run.sh", "returncode": None, "exit_signal": None},
                {"pid": 1235, "alive": True, "state": "S", "command": "arecord", "returncode": None, "exit_signal": None},
                {"pid": 1236, "alive": True, "state": "S", "command": "host_downmix.py", "returncode": None, "exit_signal": None},
                {"pid": 1237, "alive": True, "state": "S", "command": "docker exec -i openvoicestream python /tmp/realtime_pipeline.py", "returncode": None, "exit_signal": None},
            ])
            voice._last_capture_diagnostics = {"container_pipeline_alive": True}
            heartbeat = Path(directory) / "capture_heartbeat"
            downmix_heartbeat = Path(directory) / "downmix_heartbeat"

            self.assertFalse(voice.capture_health()["healthy"])
            heartbeat.write_text("4096\n", encoding="ascii")
            downmix_heartbeat.write_text("2048\n", encoding="ascii")
            self.assertTrue(voice.capture_health()["healthy"])
            old = time.time() - 10
            os.utime(heartbeat, (old, old))
            stale = voice.capture_health()
            self.assertFalse(stale["healthy"])
            self.assertEqual(stale["failure_kind"], "CAPTURE_HEARTBEAT_STALE")

            voice._owned_process_tree = Mock(return_value=[])
            exited = voice.capture_health()
            self.assertFalse(exited["healthy"])
            self.assertEqual(exited["failure_kind"], "CAPTURE_PROCESS_EXITED")

    def test_old_capture_files_are_not_a_fault_before_this_session_starts(self):
        with tempfile.TemporaryDirectory() as directory:
            out_dir = Path(directory)
            voice = SalesVoiceAdapter(out_dir=out_dir)
            (out_dir / "capture_heartbeat").write_text("4096\n", encoding="ascii")
            (out_dir / "downmix_heartbeat").write_text("2048\n", encoding="ascii")
            old = time.time() - 10
            os.utime(out_dir / "capture_heartbeat", (old, old))
            os.utime(out_dir / "downmix_heartbeat", (old, old))
            stage_dir = out_dir / "pipeline_stages"
            stage_dir.mkdir()
            (stage_dir / "arecord.json").write_text(
                '{"stage":"arecord","pid":99,"alive":false,"returncode":1}\n',
                encoding="ascii",
            )

            health = voice.capture_health()

            self.assertFalse(health["session_started"])
            self.assertEqual(health["pipeline_stages"], {})
            self.assertIsNone(health["failure_kind"])

    def test_capture_health_classifies_an_observed_stage_disappearance_as_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            voice = SalesVoiceAdapter(out_dir=Path(directory))
            voice.process = Mock(pid=1234, returncode=None)
            alive_tree = [
                {"pid": 1234, "alive": True, "state": "S", "command": "bash run.sh", "returncode": None, "exit_signal": None},
                {"pid": 1235, "alive": True, "state": "S", "command": "arecord", "returncode": None, "exit_signal": None},
                {"pid": 1236, "alive": True, "state": "S", "command": "host_downmix.py", "returncode": None, "exit_signal": None},
                {"pid": 1237, "alive": True, "state": "S", "command": "docker exec -i openvoicestream python /tmp/realtime_pipeline.py", "returncode": None, "exit_signal": None},
            ]
            voice._owned_process_tree = Mock(return_value=alive_tree)
            voice._last_capture_diagnostics = {
                "container_pipeline_known": True,
                "container_pipeline_alive": True,
            }
            (Path(directory) / "capture_heartbeat").write_text("4096\n", encoding="ascii")
            (Path(directory) / "downmix_heartbeat").write_text("2048\n", encoding="ascii")
            self.assertTrue(voice.capture_health()["healthy"])

            voice._owned_process_tree = Mock(
                return_value=[item for item in alive_tree if "host_downmix.py" not in item["command"]]
            )
            health = voice.capture_health()
            self.assertEqual(health["failure_kind"], "CAPTURE_PROCESS_EXITED")

    def test_capture_bootstrap_without_children_remains_startup_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            voice = SalesVoiceAdapter(out_dir=Path(directory))
            voice.process = Mock(pid=1234, returncode=None)
            voice._owned_process_tree = Mock(return_value=[
                {"pid": 1234, "alive": True, "state": "S", "command": "bash run.sh", "returncode": None, "exit_signal": None},
                {
                    "pid": 1235,
                    "alive": True,
                    "state": "S",
                    "command": "docker exec openvoicestream /opt/venv/bin/python /tmp/ws_broadcast.py --send-control --cmd close",
                    "returncode": None,
                    "exit_signal": None,
                },
            ])
            voice._last_capture_diagnostics = {
                "container_pipeline_known": True,
                "container_pipeline_alive": False,
            }

            health = voice.capture_health()

            self.assertFalse(health["healthy"])
            self.assertTrue(health["startup_pending"])
            self.assertFalse(health["capture_started"])
            self.assertIsNone(health["failure_kind"])
            self.assertFalse(health["stages"]["docker_exec"]["alive"])

    def test_capture_docker_stage_requires_realtime_pipeline_command(self):
        with tempfile.TemporaryDirectory() as directory:
            voice = SalesVoiceAdapter(out_dir=Path(directory))
            voice.process = Mock(pid=1234, returncode=None)
            voice._owned_process_tree = Mock(return_value=[
                {"pid": 1234, "alive": True, "state": "S", "command": "bash run.sh", "returncode": None, "exit_signal": None},
                {"pid": 1235, "alive": True, "state": "S", "command": "arecord", "returncode": None, "exit_signal": None},
                {"pid": 1236, "alive": True, "state": "S", "command": "host_downmix.py", "returncode": None, "exit_signal": None},
                {
                    "pid": 1237,
                    "alive": True,
                    "state": "S",
                    "command": "docker exec openvoicestream /tmp/ws_broadcast.py --send-control --cmd ping",
                    "returncode": None,
                    "exit_signal": None,
                },
                {
                    "pid": 1238,
                    "alive": True,
                    "state": "S",
                    "command": "docker exec -i openvoicestream /opt/venv/bin/python /tmp/realtime_pipeline.py --ws-port 8765",
                    "returncode": None,
                    "exit_signal": None,
                },
            ])

            health = voice.capture_health()

            self.assertEqual(health["stages"]["docker_exec"]["pid"], 1238)

    def test_docker_probe_unknown_is_not_classified_as_process_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            voice = SalesVoiceAdapter(out_dir=Path(directory))
            voice.process = Mock(pid=1234, returncode=None)
            voice._owned_process_tree = Mock(return_value=[
                {"pid": 1234, "alive": True, "state": "S", "command": "bash run.sh", "returncode": None, "exit_signal": None},
                {"pid": 1235, "alive": True, "state": "S", "command": "arecord", "returncode": None, "exit_signal": None},
                {"pid": 1236, "alive": True, "state": "S", "command": "host_downmix.py", "returncode": None, "exit_signal": None},
                {"pid": 1237, "alive": True, "state": "S", "command": "docker exec -i openvoicestream python /tmp/realtime_pipeline.py", "returncode": None, "exit_signal": None},
            ])
            voice._last_capture_diagnostics = {
                "container_pipeline_known": False,
                "container_pipeline_alive": False,
            }
            (Path(directory) / "capture_heartbeat").write_text("4096\n", encoding="ascii")
            (Path(directory) / "downmix_heartbeat").write_text("2048\n", encoding="ascii")
            health = voice.capture_health()
            self.assertTrue(health["healthy"])
            self.assertIsNone(health["failure_kind"])

    async def test_usb_fault_stops_only_voice_and_sets_cooldown(self):
        self.voice.running = True
        await self.supervisor._handle_voice_usb_fault()

        status = self.supervisor.status()["runtimes"]["voice"]
        self.assertFalse(self.voice.running)
        self.assertEqual(status["state"], RuntimeState.RETRY_WAIT.value)
        self.assertEqual(status["phase"], "USB_RECOVERY_COOLDOWN")
        self.assertEqual(status["retry_in_seconds"], 30.0)
        self.assertEqual(self.voice.usb_recoveries, 1)
        self.assertTrue(status["usb_recovery"]["ok"])

    async def test_usb_recovery_result_remains_visible_during_cooldown(self):
        self.voice.running = True
        await self.supervisor._handle_voice_usb_fault()
        self.supervisor.start()
        await asyncio.sleep(0.02)

        status = self.supervisor.status()["runtimes"]["voice"]
        self.assertEqual(status["phase"], "USB_RECOVERY_COOLDOWN")
        self.assertTrue(status["usb_recovery"]["ok"])
        self.assertEqual(self.voice.starts, 0)

    async def test_second_usb_fault_opens_session_circuit_breaker(self):
        self.voice.running = True
        await self.supervisor._handle_voice_usb_fault()
        self.voice.running = True
        await self.supervisor._handle_voice_usb_fault()

        status = self.supervisor.status()["runtimes"]["voice"]
        self.assertEqual(status["state"], RuntimeState.DEGRADED_USB.value)
        self.assertEqual(status["phase"], "DEGRADED_USB")
        self.assertEqual(status["fault_count"], 2)
        self.assertTrue(status["circuit_breaker_open"])
        self.assertIsNone(status["next_probe_at"])
        self.assertFalse(self.voice.running)
        self.assertEqual(self.voice.usb_recoveries, 1)

    async def test_fault_after_open_circuit_breaker_is_ignored(self):
        self.voice.running = True
        await self.supervisor._handle_voice_usb_fault()
        self.voice.running = True
        await self.supervisor._handle_voice_usb_fault()
        stops = self.voice.stops
        recoveries = self.voice.usb_recoveries

        self.voice.running = True
        await self.supervisor._handle_voice_usb_fault()

        status = self.supervisor.status()["runtimes"]["voice"]
        self.assertEqual(status["fault_count"], 2)
        self.assertTrue(status["circuit_breaker_open"])
        self.assertEqual(self.voice.stops, stops)
        self.assertEqual(self.voice.usb_recoveries, recoveries)

    async def test_open_circuit_breaker_blocks_request_and_runtime_restart(self):
        self.supervisor.start()
        await asyncio.sleep(0.03)
        starts_before_fault = self.voice.starts

        await self.supervisor._handle_voice_usb_fault()
        self.voice.running = True
        await self.supervisor._handle_voice_usb_fault()
        await self.supervisor.request_voice("en")
        await asyncio.sleep(0.05)

        status = self.supervisor.status()["runtimes"]["voice"]
        self.assertEqual(self.voice.starts, starts_before_fault)
        self.assertFalse(self.voice.running)
        self.assertEqual(status["fault_count"], 2)
        self.assertTrue(status["circuit_breaker_open"])
        self.assertEqual(status["state"], RuntimeState.DEGRADED_USB.value)
        self.assertEqual(status["phase"], "DEGRADED_USB")


if __name__ == "__main__":
    unittest.main()
