#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p conversion/logs
python3 conversion/convert_onnx_to_rknn.py \
  --onnx models/onnx/shelf_product.onnx \
  --output models/rknn/shelf_product.rknn --simulate \
  2>&1 | tee conversion/logs/shelf_product_rknn_2.3.2_simulator.log
python3 conversion/convert_onnx_to_rknn.py \
  --onnx models/onnx/held_product_v4.onnx \
  --output models/rknn/held_product_v4.rknn --simulate \
  2>&1 | tee conversion/logs/held_product_v4_rknn_2.3.2_simulator.log
