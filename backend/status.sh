#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="${DEMO_HUB_PID_FILE:-$HERE/run/demo-hub.pid}"
PORT="${DEMO_HUB_PORT:-8080}"
pid="$(cat "$PID_FILE" 2>/dev/null || true)"

if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
  echo "process: running (pid $pid)"
else
  echo "process: stopped"
fi

if command -v curl >/dev/null 2>&1; then
  health="$(curl -fsS --max-time 3 "http://127.0.0.1:$PORT/api/v1/health" 2>/dev/null || true)"
  if [[ -n "$health" ]]; then
    echo "api: healthy"
    echo "$health"
  else
    echo "api: unavailable"
  fi
else
  echo "api: unknown (curl is not installed)"
fi

