#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MULTI="$ROOT/demos/multi-camera-shelf"

for command_name in python3 curl npm; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Missing required command: $command_name" >&2
    echo "Install Python 3, curl, Node.js 18+ and npm, then retry." >&2
    exit 1
  }
done

if [[ ! -f "$ROOT/.env" ]]; then
  cp "$ROOT/.env.example" "$ROOT/.env"
  echo "Created .env from .env.example"
fi

echo "[1/4] Backend environment"
python3 -m venv "$ROOT/backend/.venv"
"$ROOT/backend/.venv/bin/python" -m pip install --upgrade pip
"$ROOT/backend/.venv/bin/python" -m pip install -r "$ROOT/backend/requirements.txt"

echo "[2/4] RKNN multi-camera environment"
python3 -m venv --system-site-packages "$MULTI/.venv"
"$MULTI/.venv/bin/python" -m pip install --upgrade pip
"$MULTI/.venv/bin/python" -m pip install -r "$MULTI/requirements-rk3588.txt"
RKNN_WHEEL="$(find "$MULTI/rknn-toolkit-lite2-packages" -maxdepth 1 -name 'rknn_toolkit_lite2-*aarch64.whl' -print -quit)"
[[ -n "$RKNN_WHEEL" ]] || { echo "Bundled RKNN Lite arm64 wheel is missing" >&2; exit 1; }
"$MULTI/.venv/bin/python" -m pip install "$RKNN_WHEEL"

echo "[3/4] Frontend production build"
cd "$ROOT/frontend"
npm ci
npm run build

echo "[4/4] Piper voice assets"
"$ROOT/scripts/download-assets.sh"

echo
echo "Bootstrap complete. The ASR image/model bundle remains external."
echo "Set VOICE_IMAGE in .env or restore the offline voice bundle, then run ./scripts/verify.sh."
