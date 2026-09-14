import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "src" / "host_downmix.py"


class HostDownmixTests(unittest.TestCase):
    def test_capture_reader_keeps_draining_during_downstream_backpressure(self):
        with tempfile.TemporaryDirectory() as directory:
            heartbeat = Path(directory) / "capture_heartbeat"
            env = dict(os.environ)
            env.update(
                {
                    "CAPTURE_HEARTBEAT": str(heartbeat),
                    "DOWNMIX_HEARTBEAT": str(Path(directory) / "downmix_heartbeat"),
                    "AUDIO_LEVEL_PATH": str(Path(directory) / "audio_level.json"),
                    "DOWNMIX_QUEUE_SECONDS": "1",
                }
            )
            process = subprocess.Popen(
                [sys.executable, str(SCRIPT)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            writer_error = []

            def feed_capture():
                try:
                    assert process.stdin is not None
                    chunk = bytes(4096)
                    for _ in range(250):
                        process.stdin.write(chunk)
                        process.stdin.flush()
                        time.sleep(0.01)
                    process.stdin.close()
                except Exception as exc:  # pragma: no cover - asserted below
                    writer_error.append(exc)

            writer = threading.Thread(target=feed_capture, daemon=True)
            writer.start()
            try:
                writer.join(timeout=6.0)
                self.assertFalse(writer.is_alive(), "capture input became backpressured")
                self.assertEqual(writer_error, [])

                deadline = time.monotonic() + 2.0
                captured = 0
                while time.monotonic() < deadline:
                    try:
                        captured = int(heartbeat.read_text(encoding="ascii").strip())
                    except (FileNotFoundError, ValueError):
                        pass
                    if captured >= 512 * 1024:
                        break
                    time.sleep(0.05)

                self.assertGreaterEqual(captured, 512 * 1024)
                self.assertIsNone(process.poll())
            finally:
                process.kill()
                process.wait(timeout=3.0)
                if process.stdout:
                    process.stdout.close()
                if process.stderr:
                    process.stderr.close()


if __name__ == "__main__":
    unittest.main()
