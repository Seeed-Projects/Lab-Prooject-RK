#!/usr/bin/env bash
set -euo pipefail
MODEL_DIR="${MODEL_DIR:-$HOME/Downloads/Qwen2.5-VL-3B_prune}"
cd "$MODEL_DIR"
echo "Starting Qwen2.5-VL-3B_prune on RK1820..."
exec rkllm3-server \
  -m llm_Qwen2.5-VL-3B_prune.rknn \
  --weight llm_Qwen2.5-VL-3B_prune.weight \
  --model2 vision_Qwen2.5-VL-3B_prune.rknn \
  --weight2 vision_Qwen2.5-VL-3B_prune.weight \
  --vocab Qwen2.5-VL-3B_prune.tokenizer.gguf \
  --embed Qwen2.5-VL-3B_prune.embed.bin \
  --host 0.0.0.0 --port 8080 \
  -c 768 --n_predict 512 \
  --core-mask 0xff --core-mask2 0xff \
  --repeat-penalty 1.1 --presence-penalty 1.0 --frequency-penalty 1.0 \
  --top-k 1 --top-p 0.8 --temp 0.8 \
  --img-start "<|vision_start|>" \
  --img-end "<|vision_end|>" \
  --img-content "<|image_pad|>" \
  --img-width 392 --img-height 392
