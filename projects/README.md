# Seeed AI Lab Projects

This directory contains the three RK182x projects published as independent Seeed AI Lab downloads. The existing RK3588 retail showcase remains the fourth project at the repository root.

| Project | Target | Model | Download asset | Start page |
| --- | --- | --- | --- | --- |
| Multimodal Retail AI Demo Center | RK3588 | RKNN + local voice models | `seeed-ai-lab-retail-ai-center-v<version>.tar.gz` | `http://<board-ip>:8080` |
| RK1828 Qwen3-4B Long Context Benchmark | RK1828 | Qwen3-4B | `seeed-ai-lab-rk1828-4b-benchmark-v<version>.tar.gz` | `http://<board-ip>:7862` |
| RK1820 Qwen3-1.7B Long Context Benchmark | RK1820 | Qwen3-1.7B | `seeed-ai-lab-rk1820-qwen3-1p7b-benchmark-v<version>.tar.gz` | `http://<board-ip>:7863` |
| RK1820 Vision Story Studio | RK1820 | Qwen2.5-VL-3B_prune | `seeed-ai-lab-rk1820-vlm-creative-v<version>.tar.gz` | `http://<board-ip>:7861` |

The RK1820 text model currently available in the source environment is **Qwen3-1.7B**. The directory name `rk1820-1p5b-benchmark` is retained for the first implementation pass so existing work can be reviewed without moving files; the release asset uses the truthful `qwen3-1p7b` ID.

Each Release Asset contains one project only. Customers should download the asset shown on the matching Seeed AI Lab page instead of using GitHub's repository ZIP. Generate the assets locally with:

```bash
./tools/package-project.sh --all --version 1.0.0
```

Validate an asset before publishing it:

```bash
./tools/check-project-package.sh dist/seeed-ai-lab-rk1820-vlm-creative-v1.0.0.tar.gz
```

The model binaries are intentionally kept outside Git. Set `MODEL_DIR` to the model directory supplied for the matching board before starting a RK182x project. The project README lists the exact filenames expected by the launcher.
