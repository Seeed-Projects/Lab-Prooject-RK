#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-.venv}"
APP_PORT="${APP_PORT:-7861}"
export RKLLM_BASE_URL="${RKLLM_BASE_URL:-http://127.0.0.1:8080}"
if [ ! -d "$VENV_DIR" ]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip >/dev/null
python -m pip install -r requirements.txt
exec uvicorn app:app --host 0.0.0.0 --port "$APP_PORT"
