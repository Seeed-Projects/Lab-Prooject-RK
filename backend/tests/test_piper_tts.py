import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.piper_tts import PiperTTS, PiperTTSUnavailable


class FakeProcess:
    def __init__(self):
        self.returncode = 0

    async def communicate(self, _input=None):
        return b"", b""

    async def wait(self):
        return self.returncode

    def kill(self):
        self.returncode = -9

    def terminate(self):
        self.returncode = -15


class PiperTTSTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.piper = root / "piper"
        self.model = root / "en_US-amy-low.onnx"
        self.player = root / "paplay"
        for executable in (self.piper, self.player):
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
        self.model.write_bytes(b"model")

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_tts(self):
        return PiperTTS(
            piper_bin=str(self.piper),
            model_path=str(self.model),
            player_bin=str(self.player),
            sink="test-es8311-sink",
            length_scale=1.12,
        )

    async def test_rejects_empty_and_overlong_text(self):
        tts = self.make_tts()
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            await tts.speak("   ")
        with self.assertRaisesRegex(ValueError, "at most 800"):
            await tts.speak("x" * 801)

    async def test_missing_model_returns_unavailable_status(self):
        tts = PiperTTS(
            piper_bin=str(self.piper),
            model_path=str(self.model.with_name("missing.onnx")),
            player_bin=str(self.player),
        )
        self.assertFalse(tts.status()["configured"])
        with self.assertRaises(PiperTTSUnavailable):
            await tts.speak("Hello")

    async def test_uses_amy_model_slow_rate_and_es8311_sink(self):
        calls = []

        async def fake_subprocess(*args, **kwargs):
            calls.append((args, kwargs))
            if str(args[0]) == str(self.piper):
                output_path = Path(args[args.index("--output_file") + 1])
                output_path.write_bytes(b"RIFF" + (b"\0" * 64))
            return FakeProcess()

        tts = self.make_tts()
        with patch("app.piper_tts.asyncio.create_subprocess_exec", side_effect=fake_subprocess):
            status = await tts.speak("There are twelve bottles.")

        self.assertTrue(status["ok"])
        self.assertFalse(status["speaking"])
        self.assertEqual(len(calls), 2)
        piper_args = calls[0][0]
        self.assertEqual(piper_args[piper_args.index("--model") + 1], str(self.model))
        self.assertEqual(piper_args[piper_args.index("--length_scale") + 1], "1.12")
        player_args = calls[1][0]
        self.assertIn("--device=test-es8311-sink", player_args)
        player_env = calls[1][1]["env"]
        self.assertEqual(player_env["XDG_RUNTIME_DIR"], "/run/user/1000")
        self.assertEqual(player_env["PULSE_SERVER"], "unix:/run/user/1000/pulse/native")


if __name__ == "__main__":
    unittest.main()
