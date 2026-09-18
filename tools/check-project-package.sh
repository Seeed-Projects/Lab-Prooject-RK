#!/usr/bin/env bash
set -euo pipefail

archive="${1:-}"
[ -n "$archive" ] && [ -f "$archive" ] || { echo "Usage: $0 <archive.tar.gz>" >&2; exit 2; }

tar -tzf "$archive" >/dev/null
mapfile -t entries < <(tar -tzf "$archive")
top_levels=()
for entry in "${entries[@]}"; do
  top="${entry%%/*}"
  [ -n "$top" ] || continue
  case " ${top_levels[*]} " in *" $top "*) ;; *) top_levels+=("$top") ;; esac
done
[ "${#top_levels[@]}" -eq 1 ] || { echo "Expected one top-level directory" >&2; exit 1; }

for forbidden in projects tools .git .venv __pycache__; do
  if printf '%s\n' "${entries[@]}" | grep -Eq "(^|/)${forbidden}(/|$)"; then
    echo "Forbidden path found: $forbidden" >&2
    exit 1
  fi
done
printf '%s\n' "${entries[@]}" | grep -Eq '/project\.json$' || { echo "Missing project.json" >&2; exit 1; }
printf '%s\n' "${entries[@]}" | grep -Eq '/README\.md$' || { echo "Missing README.md" >&2; exit 1; }
printf '%s\n' "${entries[@]}" | grep -Eq '/(start_ui\.sh|scripts/start\.sh)$' || { echo "Missing project entrypoint" >&2; exit 1; }
if printf '%s\n' "${entries[@]}" | grep -Eq '(\.pyc$|\.bak|\.backup|\.before_)'; then
  echo "Build artifacts or historical backups found" >&2
  exit 1
fi
echo "Valid package: $archive"
