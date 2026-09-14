#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL_DIR="$ROOT/models/piper"
VOICE_DIR="$MODEL_DIR/voices"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

PIPER_URL="https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_aarch64.tar.gz"
MODEL_URL="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/low/en_US-amy-low.onnx"
CONFIG_URL="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/low/en_US-amy-low.onnx.json"
PIPER_SHA="0c44a6360a21367b5d03b8656c3e274f4de715f6533245d8c1f17c242631912b"
MODEL_SHA="a5a91abb7de0f104358a25aded480ddacf1ff0762886325886ec406a2e86aab3"
CONFIG_SHA="2250a9a605b8dc35a116717fadc5056695dd809e34a15d02f72a0f52d53d3ebb"

check_file() {
  local path="$1" expected="$2"
  [[ -f "$path" ]] && [[ "$(sha256sum "$path" | awk '{print $1}')" == "$expected" ]]
}

download() {
  local url="$1" output="$2"
  curl -fL --retry 3 --connect-timeout 15 "$url" -o "$output"
}

mkdir -p "$MODEL_DIR" "$VOICE_DIR"
if ! check_file "$MODEL_DIR/piper" "$PIPER_SHA"; then
  echo "Downloading Piper arm64 runtime..."
  download "$PIPER_URL" "$TMP_DIR/piper.tar.gz"
  tar -xzf "$TMP_DIR/piper.tar.gz" -C "$TMP_DIR"
  [[ -x "$TMP_DIR/piper/piper" ]] || { echo "Piper archive layout is invalid" >&2; exit 1; }
  check_file "$TMP_DIR/piper/piper" "$PIPER_SHA" || { echo "Piper executable checksum mismatch" >&2; exit 1; }
  cp -a "$TMP_DIR/piper/." "$MODEL_DIR/"
fi

if ! check_file "$VOICE_DIR/en_US-amy-low.onnx" "$MODEL_SHA"; then
  echo "Downloading Piper en_US-amy-low model..."
  download "$MODEL_URL" "$TMP_DIR/en_US-amy-low.onnx"
  check_file "$TMP_DIR/en_US-amy-low.onnx" "$MODEL_SHA" || { echo "Voice model checksum mismatch" >&2; exit 1; }
  install -m 0644 "$TMP_DIR/en_US-amy-low.onnx" "$VOICE_DIR/en_US-amy-low.onnx"
fi

if ! check_file "$VOICE_DIR/en_US-amy-low.onnx.json" "$CONFIG_SHA"; then
  echo "Downloading Piper voice config..."
  download "$CONFIG_URL" "$TMP_DIR/en_US-amy-low.onnx.json"
  check_file "$TMP_DIR/en_US-amy-low.onnx.json" "$CONFIG_SHA" || { echo "Voice config checksum mismatch" >&2; exit 1; }
  install -m 0644 "$TMP_DIR/en_US-amy-low.onnx.json" "$VOICE_DIR/en_US-amy-low.onnx.json"
fi

chmod +x "$MODEL_DIR/piper"
echo "Piper assets are ready in $MODEL_DIR"
