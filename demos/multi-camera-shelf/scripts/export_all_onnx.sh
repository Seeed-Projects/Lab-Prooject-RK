#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 conversion/export_onnx.py \
  --model models/source_pt/shelf_product.pt \
  --output models/onnx/shelf_product.onnx --imgsz 640 --opset 12
python3 conversion/export_onnx.py \
  --model models/source_pt/held_product_v4.pt \
  --output models/onnx/held_product_v4.onnx --imgsz 320 --opset 12

