#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_PY="$ROOT/backend/.venv/bin/python"
[[ -x "$BACKEND_PY" ]] || BACKEND_PY=python3

echo "[1/8] Python compilation"
cd "$ROOT/backend"
"$BACKEND_PY" -m py_compile app/*.py adapters/*.py controllers/*.py tools/*.py

echo "[2/8] Backend tests"
"$BACKEND_PY" -m unittest discover -s tests -p 'test_*.py' -q

echo "[3/8] Voice pipeline tests"
cd "$ROOT/services/voice-pipeline"
"$BACKEND_PY" -m unittest discover -s tests -p 'test_*.py' -q

echo "[4/8] Multi-camera tests and package check"
MULTI_PY="$ROOT/demos/multi-camera-shelf/.venv/bin/python"
[[ -x "$MULTI_PY" ]] || { echo "Multi-camera environment is missing; run ./scripts/bootstrap.sh" >&2; exit 1; }
cd "$ROOT/demos/multi-camera-shelf"
"$MULTI_PY" -m pytest -q
"$MULTI_PY" tools/check_package.py

echo "[5/8] Frontend build"
cd "$ROOT/frontend"
[[ -d node_modules ]] || { echo "frontend/node_modules is missing; run ./scripts/bootstrap.sh" >&2; exit 1; }
npm run build

echo "[6/8] Shell syntax"
cd "$ROOT"
while IFS= read -r script; do bash -n "$script"; done < <(find scripts backend services/voice-pipeline -maxdepth 2 -type f -name '*.sh' -print)

echo "[7/8] Versioned model checks"
test -s "$ROOT/demos/multi-camera-shelf/models/rknn/shelf_product.rknn"
test -s "$ROOT/demos/multi-camera-shelf/models/rknn/held_product_v4.rknn"
test -s "$ROOT/demos/multi-camera-shelf/input/demo.mp4"

echo "[8/8] Optional runtime checks"
command -v docker >/dev/null 2>&1 && echo "docker: found" || echo "docker: missing (voice demos unavailable)"
command -v go2rtc >/dev/null 2>&1 && echo "go2rtc: found" || echo "go2rtc: missing (MJPEG fallback will be used)"
arecord -l 2>/dev/null | grep -q 'Array' && echo "ReSpeaker: found" || echo "ReSpeaker: not detected"

echo "Verification complete"
