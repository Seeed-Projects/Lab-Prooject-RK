import tempfile
import unittest
from pathlib import Path

from app.adapters.retail_shelf import RetailShelfAdapter
from app.adapters.xvf3800_asr import XVF3800ASRAdapter
from app.domain import Manifest, ValidationError


def manifest(root: Path, adapter_type: str, parameters=None) -> Manifest:
    return Manifest(
        name="test-demo",
        type="streaming/test",
        entry=("bash", "run.sh"),
        port=None,
        description="test",
        project_root=root,
        adapter={"type": adapter_type},
        parameters=parameters or {},
    )


class AdapterTests(unittest.TestCase):
    def test_asr_maps_turn_and_builds_whitelisted_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = XVF3800ASRAdapter(manifest(root, "process-jsonl-websocket", {
                "language": {"type": "string", "enum": ["zh", "en"], "default": "zh"},
                "recluster_every": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
            }))
            spec = adapter.build_command(root / "run", {"language": "en", "recluster_every": 5})
            self.assertIn("en", spec.argv)
            self.assertIn("5", spec.argv)
            events = adapter.parse_line('{"type":"turn","speaker":1,"text":"hello"}', "stdout")
            self.assertEqual(events[0][1], "asr.turn")
            self.assertEqual(events[0][2]["speaker"], 1)
            with self.assertRaises(ValidationError):
                adapter.build_command(root / "run", {"language": "invalid"})

    def test_retail_extracts_fps_and_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = RetailShelfAdapter(manifest(Path(directory), "process-file"))
            events = adapter.parse_line("Frame 42 FPS: 17.5", "stdout")
            self.assertEqual({event[1] for event in events}, {"metrics.fps", "video.progress"})


if __name__ == "__main__":
    unittest.main()

