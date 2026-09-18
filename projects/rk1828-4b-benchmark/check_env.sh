#!/usr/bin/env bash
set -u
MODEL_DIR="${MODEL_DIR:-$HOME/Downloads/Qwen3-4B}"
echo "===== PCIe ====="
lspci -nn | grep -Ei "182|Processing accelerators" || true
echo
echo "===== RKNN SMI ====="
rknn-smi info -l 2>/dev/null || sudo rknn-smi info -l || true
echo
echo "===== RKNN Version ====="
rknn-smi version 2>/dev/null || true
echo
echo "===== Model Files ====="
ls -lh "$MODEL_DIR" || true
echo
echo "===== Model Build Strings ====="
if [ -f "$MODEL_DIR/Qwen3-4B.rknn" ]; then
  strings "$MODEL_DIR/Qwen3-4B.rknn" | grep -Eai "compiler version|rknn3-toolkit|RK1820|RK1828" | head -20 || true
fi
