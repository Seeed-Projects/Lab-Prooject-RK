#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="${DEMO_HUB_PID_FILE:-$HERE/run/demo-hub.pid}"

if [[ ! -f "$PID_FILE" ]]; then
  echo "Demo Hub is stopped (no pid file)"
  exit 0
fi
pid="$(cat "$PID_FILE" 2>/dev/null || true)"
if [[ ! "$pid" =~ ^[0-9]+$ ]] || ! kill -0 "$pid" 2>/dev/null; then
  rm -f "$PID_FILE"
  echo "Demo Hub is stopped (stale pid file removed)"
  exit 0
fi

kill -TERM "$pid"
for _ in {1..80}; do
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$PID_FILE"
    echo "Demo Hub stopped"
    exit 0
  fi
  sleep 0.25
done

echo "Graceful stop timed out; sending SIGKILL" >&2
kill -KILL "$pid" 2>/dev/null || true
rm -f "$PID_FILE"
echo "Demo Hub stopped (forced)"

