#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON_BIN="python3"
if [[ -x .venv/bin/python ]]; then
  PYTHON_BIN=".venv/bin/python"
fi
"$PYTHON_BIN" tools/check_package.py --require-rknn
"$PYTHON_BIN" app/infer_video_rknn.py --config configs/runtime.json "$@"

