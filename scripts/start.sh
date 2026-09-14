#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "$ROOT/.env" ]]; then
  set -a
  source "$ROOT/.env"
  set +a
fi
export RETAIL_AI_ROOT="$ROOT"

if systemctl cat recomputer-retail-ai.service >/dev/null 2>&1; then
  sudo systemctl start recomputer-retail-ai.service
  sudo systemctl --no-pager --full status recomputer-retail-ai.service | sed -n '1,14p'
else
  "$ROOT/backend/start.sh"
fi
