#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${ROOT_DIR}/dist"
VERSION=""
SELECTED=()

usage() {
  echo "Usage: $0 --project <id> [--project <id> ...] --version <version>"
  echo "       $0 --all --version <version>"
  exit 2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --project) [ "$#" -ge 2 ] || usage; SELECTED+=("$2"); shift 2 ;;
    --all) SELECTED=(retail-ai-center rk1828-4b-benchmark rk1820-qwen3-1p7b-benchmark rk1820-vlm-creative); shift ;;
    --version) [ "$#" -ge 2 ] || usage; VERSION="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "Unknown option: $1" >&2; usage ;;
  esac
done

[ -n "$VERSION" ] || usage
[ "${#SELECTED[@]}" -gt 0 ] || usage
mkdir -p "$DIST_DIR"

project_source() {
  case "$1" in
    retail-ai-center) echo "$ROOT_DIR" ;;
    rk1828-4b-benchmark) echo "$ROOT_DIR/projects/rk1828-4b-benchmark" ;;
    rk1820-qwen3-1p7b-benchmark) echo "$ROOT_DIR/projects/rk1820-1p5b-benchmark" ;;
    rk1820-vlm-creative) echo "$ROOT_DIR/projects/rk1820-vlm-creative" ;;
    *) echo "Unknown project: $1" >&2; return 1 ;;
  esac
}

for project in "${SELECTED[@]}"; do
  source_dir="$(project_source "$project")"
  staging="$(mktemp -d)"
  package_root="${staging}/seeed-ai-lab-${project}-v${VERSION}"
  mkdir -p "$package_root"

  if [ "$project" = "retail-ai-center" ]; then
    tar -C "$ROOT_DIR" \
      --exclude='.git' --exclude='.github' --exclude='projects' \
      --exclude='tools' --exclude='dist' \
      --exclude='**/.venv' --exclude='**/__pycache__' --exclude='**/*.pyc' \
      --exclude='**/.pytest_cache' --exclude='**/*.bak*' \
      -cf - . | tar -C "$package_root" -xf -
  else
    tar -C "$source_dir" \
      --exclude='./.venv' --exclude='./__pycache__' --exclude='*.pyc' \
      --exclude='./.pytest_cache' --exclude='*.bak*' \
      --exclude='./static/*.backup*' --exclude='./static/*.before_*' \
      -cf - . | tar -C "$package_root" -xf -
  fi

  archive="${DIST_DIR}/seeed-ai-lab-${project}-v${VERSION}.tar.gz"
  tar -C "$staging" -czf "$archive" "$(basename "$package_root")"
  rm -rf "$staging"
  echo "Created $archive"
done
