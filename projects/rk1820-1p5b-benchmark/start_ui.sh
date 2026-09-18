#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-.venv}"
APP_PORT="${APP_PORT:-7863}"
export RKLLM_BASE_URL="${RKLLM_BASE_URL:-http://127.0.0.1:8080}"
export MODEL_NAME="${MODEL_NAME:-Qwen3-1.7B}"
export MODEL_QUANTIZATION="${MODEL_QUANTIZATION:-W4A16}"
export SERVER_CONTEXT="${SERVER_CONTEXT:-25088}"
export MODEL_MAX_CONTEXT="${MODEL_MAX_CONTEXT:-25088}"
export DEVICE_LABEL="${DEVICE_LABEL:-RK1820}"
export PRODUCT_LABEL="${PRODUCT_LABEL:-reComputer RK3576 Dev Kit + RK1820 AI Accelerator}"
if [ ! -d "$VENV_DIR" ]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip >/dev/null
python -m pip install -r requirements.txt
printf '\nOpen the demo in your browser:\n  http://<RK3576-IP>:%s\n\n' "$APP_PORT"
exec uvicorn app:app --host 0.0.0.0 --port "$APP_PORT"
