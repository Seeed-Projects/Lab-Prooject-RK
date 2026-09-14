from __future__ import annotations

import os
from pathlib import Path


PACKAGE_ROOT = Path(
    os.getenv("RETAIL_AI_ROOT", Path(__file__).resolve().parents[2])
).expanduser().resolve()
BACKEND_ROOT = PACKAGE_ROOT / "backend"
FRONTEND_ROOT = PACKAGE_ROOT / "frontend"
MULTI_CAMERA_ROOT = Path(
    os.getenv("MULTI_CAMERA_ROOT", PACKAGE_ROOT / "demos" / "multi-camera-shelf")
).expanduser().resolve()
VOICE_PIPELINE_ROOT = Path(
    os.getenv("VOICE_PIPELINE_ROOT", PACKAGE_ROOT / "services" / "voice-pipeline")
).expanduser().resolve()
MODEL_ROOT = Path(os.getenv("MODEL_ROOT", PACKAGE_ROOT / "models")).expanduser().resolve()
