#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON_BIN="${PYTHON_BIN:-python3}"
"$PYTHON_BIN" -m venv --system-site-packages .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-rk3588.txt

if [[ $# -ge 1 ]]; then
  .venv/bin/python -m pip install "$1"
else
  echo
  echo "RKNNLite wheel was not installed."
  echo "After running scripts/probe.sh, obtain the matching Rockchip"
  echo "rknn_toolkit_lite2 wheel and rerun:"
  echo "  ./scripts/install_runtime.sh /path/to/rknn_toolkit_lite2-*.whl"
fi

.venv/bin/python - <<'PY'
for name in ("numpy", "cv2", "rknnlite"):
    try:
        module = __import__(name)
        print(f"{name}: OK version={getattr(module, '__version__', 'unknown')}")
    except Exception as exc:
        print(f"{name}: NOT READY ({exc})")
PY
