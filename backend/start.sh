#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="${DEMO_HUB_PID_FILE:-$HERE/run/demo-hub.pid}"
LOG_FILE="${DEMO_HUB_LOG_FILE:-$HERE/run/demo-hub.log}"
PORT="${DEMO_HUB_PORT:-8080}"
HOST="${DEMO_HUB_HOST:-0.0.0.0}"
UVICORN_BIN="${DEMO_HUB_UVICORN:-$HERE/.venv/bin/uvicorn}"

mkdir -p "$(dirname "$PID_FILE")" "$(dirname "$LOG_FILE")"
if [[ ! -x "$UVICORN_BIN" ]]; then
  echo "Demo Hub dependency missing: $UVICORN_BIN" >&2
  echo "Run: python3 -m venv $HERE/.venv && $HERE/.venv/bin/pip install -r $HERE/requirements.txt" >&2
  exit 1
fi
if [[ ! -f "$HERE/../frontend/dist/index.html" ]]; then
  echo "Frontend build missing: $HERE/../frontend/dist/index.html" >&2
  echo "Run: cd $HERE/../frontend && npm install && npm run build" >&2
  exit 1
fi

export PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}"
export DEMO_HUB_DATA_DIR="${DEMO_HUB_DATA_DIR:-$HERE/var}"
export DEMO_HUB_PLUGIN_DIR="${DEMO_HUB_PLUGIN_DIR:-$HERE/plugins}"

if [[ "${1:-}" == "--foreground" ]]; then
  # systemd owns the process lifecycle; replace this shell without touching
  # any PID left by an earlier manual launch.
  echo "$$" > "$PID_FILE"
  exec "$UVICORN_BIN" app.main:app --host "$HOST" --port "$PORT" --proxy-headers
fi

if [[ -f "$PID_FILE" ]]; then
  old_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ "$old_pid" =~ ^[0-9]+$ ]] && kill -0 "$old_pid" 2>/dev/null; then
    "$HERE/stop.sh"
  else
    rm -f "$PID_FILE"
  fi
fi

nohup "$UVICORN_BIN" app.main:app --host "$HOST" --port "$PORT" --proxy-headers >> "$LOG_FILE" 2>&1 &
child_pid=$!
echo "$child_pid" > "$PID_FILE"
for _ in {1..20}; do
  if kill -0 "$child_pid" 2>/dev/null; then
    echo "Demo Hub started (pid $child_pid); http://$(hostname -I 2>/dev/null | awk '{print $1}'):$PORT"
    exit 0
  fi
  if ! kill -0 "$child_pid" 2>/dev/null; then
    echo "Demo Hub failed to start; see $LOG_FILE" >&2
    exit 1
  fi
  sleep 0.25
done
echo "Demo Hub start timed out; see $LOG_FILE" >&2
exit 1
