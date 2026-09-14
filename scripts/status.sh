#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if systemctl cat recomputer-retail-ai.service >/dev/null 2>&1; then
  systemctl --no-pager --full status recomputer-retail-ai.service | sed -n '1,18p' || true
fi
"$ROOT/backend/status.sh"

for endpoint in \
  http://127.0.0.1:8621/health \
  http://127.0.0.1:8001/health; do
  if curl -fsS --max-time 3 "$endpoint" >/dev/null 2>&1; then
    echo "$endpoint: healthy"
  else
    echo "$endpoint: unavailable"
  fi
done
