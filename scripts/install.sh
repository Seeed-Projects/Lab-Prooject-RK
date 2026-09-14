#!/usr/bin/env bash
set -euo pipefail

[[ "${EUID:-$(id -u)}" -eq 0 ]] || { echo "Run with sudo: sudo ./scripts/install.sh" >&2; exit 1; }
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OWNER="$(stat -c '%U' "$ROOT")"
OWNER_GROUP="$(stat -c '%G' "$ROOT")"
OWNER_UID="$(id -u "$OWNER")"
TEMPLATE="$ROOT/deploy/systemd/recomputer-retail-ai.service.in"
TARGET=/etc/systemd/system/recomputer-retail-ai.service

[[ -x "$ROOT/backend/.venv/bin/uvicorn" ]] || { echo "Backend environment missing; run ./scripts/bootstrap.sh first" >&2; exit 1; }
[[ -f "$ROOT/frontend/dist/index.html" ]] || { echo "Frontend build missing; run ./scripts/bootstrap.sh first" >&2; exit 1; }

sed -e "s|@ROOT@|$ROOT|g" -e "s|@USER@|$OWNER|g" -e "s|@GROUP@|$OWNER_GROUP|g" -e "s|@UID@|$OWNER_UID|g" "$TEMPLATE" > "$TARGET"
chmod 0644 "$TARGET"
sed -e "s|@ROOT@|$ROOT|g" "$ROOT/deploy/systemd/recomputer-xvf3800-recover.service.in" > /etc/systemd/system/recomputer-xvf3800-recover.service
sed -e "s|@ROOT@|$ROOT|g" "$ROOT/deploy/systemd/recomputer-xvf3800-recover.path.in" > /etc/systemd/system/recomputer-xvf3800-recover.path
chmod 0644 /etc/systemd/system/recomputer-xvf3800-recover.service /etc/systemd/system/recomputer-xvf3800-recover.path
systemctl daemon-reload
systemctl enable --now recomputer-retail-ai.service
systemctl enable --now recomputer-xvf3800-recover.path

echo "Installed recomputer-retail-ai.service for user $OWNER"
echo "Installed one-attempt-per-boot XVF3800 recovery watcher"
echo "Open http://$(hostname -I 2>/dev/null | awk '{print $1}'):8080"
