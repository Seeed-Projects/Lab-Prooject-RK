# RK1820 Qwen3-1.7B Long Context Benchmark

This is a standalone Seeed AI Lab project for the reComputer RK3576 with an RK1820 accelerator. It provides a fast first-turn test, long-context retrieval, and multi-turn memory in one local web UI.

The source environment currently contains Firefly's **Qwen3-1.7B** RK1820 package. The user-facing project title should therefore use 1.7B until a separate 1.5B model package is supplied.

## Hardware and model

- Board: reComputer RK3576 + RK1820
- Model: Qwen3-1.7B, W4A16, 25088-token model context
- Model directory: `~/Downloads/Firefly-Qwen3-1.7B-RK1820` by default
- UI: `http://<board-ip>:7863`

Expected model files are `Qwen3-1.7B-25088.rknn`, `Qwen3-1.7B.weight`, `Qwen3-1.7B.tokenizer.gguf`, and `Qwen3-1.7B.embed.bin`.

## Run

```bash
chmod +x *.sh
MODEL_DIR=/path/to/Firefly-Qwen3-1.7B-RK1820 ./start_model_server.sh
```

Keep the model server running and open another terminal:

```bash
./start_ui.sh
```

The NIAH and memory controls are bounded by the configured model context. The displayed token rate is measured through the local streaming API and is separate from an `rknn3_session_test` result.
