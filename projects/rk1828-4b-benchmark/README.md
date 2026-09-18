# RK1828 Qwen3-4B Long Context Benchmark

This is a standalone Seeed AI Lab project for the reComputer RK3576 with an RK1828 accelerator. It combines first-turn speed, needle-in-a-haystack retrieval, and multi-turn conversation memory in one local web UI.

## Hardware and model

- Board: reComputer RK3576 + RK1828
- Model: Qwen3-4B, converted for RKNN3/RK1828
- Model directory: `~/Downloads/Qwen3-4B` by default
- UI: `http://<board-ip>:7862`

Expected model files are `Qwen3-4B.rknn`, `Qwen3-4B.weight`, `Qwen3-4B.tokenizer.gguf`, and the matching embedding file used by the installed RKLLM runtime.

## Run

```bash
chmod +x *.sh
./check_env.sh
MODEL_DIR=/path/to/Qwen3-4B ./start_model_server.sh
```

Keep the model server running and open a second terminal:

```bash
MODEL_NAME=Qwen3-4B ./start_ui.sh
```

Set `CTX_SIZE` and `SERVER_CONTEXT` to the same value when testing a smaller context, for example `3072`.

The UI reports application-level streaming measurements. The benchmark page also provides a needle retrieval sweep and a persistent KV-cache memory test.
