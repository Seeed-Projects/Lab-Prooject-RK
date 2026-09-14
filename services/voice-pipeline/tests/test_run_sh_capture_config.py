from pathlib import Path
import unittest


RUN_SH = Path(__file__).resolve().parents[1] / "run.sh"


class RunShCaptureConfigTests(unittest.TestCase):
    def test_live_capture_uses_explicit_alsa_buffering(self) -> None:
        script = RUN_SH.read_text(encoding="utf-8")

        self.assertIn('CAPTURE_PERIOD_US="${CAPTURE_PERIOD_US:-100000}"', script)
        self.assertIn('CAPTURE_BUFFER_US="${CAPTURE_BUFFER_US:-2000000}"', script)
        self.assertIn('--period-time "$CAPTURE_PERIOD_US"', script)
        self.assertIn('--buffer-time "$CAPTURE_BUFFER_US"', script)

    def test_capture_log_identifies_effective_config_and_downmix_generation(self) -> None:
        script = RUN_SH.read_text(encoding="utf-8")

        self.assertIn("ALSA capture period=${CAPTURE_PERIOD_US}us", script)
        self.assertIn("buffer=${CAPTURE_BUFFER_US}us downmix=v2-buffered", script)

    def test_live_capture_diagnostics_are_scoped_to_the_current_session(self) -> None:
        script = RUN_SH.read_text(encoding="utf-8")

        self.assertIn('rm -f "$PIPELINE_STAGES"/*.json', script)
        self.assertIn(': >"$OUT_DIR/arecord.stderr.log"', script)
        self.assertIn(': >"$OUT_DIR/host_downmix.stderr.log"', script)


if __name__ == "__main__":
    unittest.main()
