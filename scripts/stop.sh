#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if systemctl cat recomputer-retail-ai.service >/dev/null 2>&1; then
  sudo systemctl stop recomputer-retail-ai.service
  echo "recomputer-retail-ai.service stopped"
else
  "$ROOT/backend/stop.sh"
fi
