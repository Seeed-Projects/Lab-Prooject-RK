# RK1820 Vision Story Studio

Vision Story Studio turns the RK1820 3B VLM test into a small creative workstation. Give it an image and choose a direction: a postcard, a short story, a visual scene description, or a set of creative prompts. The image and prompt stay on the device and the response streams from the local Qwen2.5-VL model.

## Hardware and model

- Board: reComputer RK3576 + RK1820
- Model: Qwen2.5-VL-3B_prune
- Model directory: `~/Downloads/Qwen2.5-VL-3B_prune` by default
- UI: `http://<board-ip>:7861`

Expected model files are `llm_Qwen2.5-VL-3B_prune.rknn`, `llm_Qwen2.5-VL-3B_prune.weight`, `vision_Qwen2.5-VL-3B_prune.rknn`, `vision_Qwen2.5-VL-3B_prune.weight`, `Qwen2.5-VL-3B_prune.tokenizer.gguf`, and the matching embedding file.

## Run

```bash
chmod +x *.sh
MODEL_DIR=/path/to/Qwen2.5-VL-3B_prune ./start_vlm_server.sh
```

Keep the VLM server running and open a second terminal:

```bash
./start_ui.sh
```

Upload a JPG, PNG, or WEBP image, choose a creative preset, and press `Create Story`. You can also edit the prompt directly.
