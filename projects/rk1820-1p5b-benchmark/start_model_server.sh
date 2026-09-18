#!/usr/bin/env bash
set -euo pipefail
MODEL_DIR="${MODEL_DIR:-$HOME/Downloads/Firefly-Qwen3-1.7B-RK1820}"
CTX_SIZE="${CTX_SIZE:-25088}"
PORT="${RKLLM_PORT:-8080}"
N_PREDICT="${N_PREDICT:-512}"
cd "$MODEL_DIR"
echo "============================================================"
echo "Starting Qwen3-1.7B on RK1820"
echo "Model directory : $MODEL_DIR"
echo "Context size    : $CTX_SIZE"
echo "Server port     : $PORT"
echo "============================================================"
exec rkllm3-server \
  -m Qwen3-1.7B-25088.rknn \
  --weight Qwen3-1.7B.weight \
  --vocab Qwen3-1.7B.tokenizer.gguf \
  --embed Qwen3-1.7B.embed.bin \
  --host 0.0.0.0 \
  --port "$PORT" \
  -c "$CTX_SIZE" \
  --n_predict "$N_PREDICT" \
  --core-mask 0xff \
  --reasoning off \
  --repeat-penalty 1.1 \
  --presence-penalty 1.0 \
  --frequency-penalty 1.0 \
  --top-k 1 \
  --top-p 0.8 \
  --temp 0.8
