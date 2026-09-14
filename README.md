# Multimodal Retail AI Demo Center on reComputer RK3588

One local-first application that combines visual inventory monitoring, multi-stream RKNN inference, and voice analytics on a reComputer RK3588.

## System architecture

![Physical deployment and project architecture](media/architecture/system-architecture.svg)

reCamera supplies detected video and authoritative product state, while ReSpeaker supplies far-field audio. The reComputer RK3588 runs the Vue/FastAPI application, all three local AI workflows, and the model runtimes; a browser provides the unified operator interface, and Piper returns spoken inventory answers through the attached speaker.

## Included demos

| Demo | Input | Local AI | Result |
| --- | --- | --- | --- |
| Smart Shelf, single camera | reCamera RTSP + product WebSocket, ReSpeaker | English ASR, RKLLM intent routing, Piper TTS | Live inventory, missing products, grounded spoken answers |
| Smart Shelf, multi-camera | Four independent video streams | RKNN object detection on RK3588 NPU | Four live feeds, shelf events, restock alerts |
| Sales Conversation Analysis | ReSpeaker XVF3800 | Speaker diarization, ASR, local RKLLM | Live transcript, speaker turns, conversation summary |

### Smart Shelf: single camera

![Single-camera inventory and voice assistant](media/screenshots/single-camera.png)

The assistant never answers inventory questions from stale data. If reCamera data is missing, uninitialized, or expired, it reports that state instead of inventing a quantity. RTSP is bridged to WebRTC when go2rtc is installed, with MJPEG as a fallback.

### Smart Shelf: multi-camera

![Four-stream shelf monitoring](media/screenshots/multi-camera.png)

### Sales Conversation Analysis

![Sales conversation analysis](media/screenshots/sales-voice.png)

## Hardware

- reComputer based on RK3588, tested on Debian 12 arm64
- reCamera reachable over USB networking or Ethernet
- ReSpeaker XVF3800 USB microphone array
- ES8311 3.5 mm audio output for spoken answers
- At least 8 GB RAM and 12 GB free storage when voice model images are installed

See [hardware setup](docs/hardware-setup.md) for cabling and health checks.

## Quick start

```bash
git clone https://github.com/Seeed-Projects/Lab-Prooject-RK.git
cd Lab-Prooject-RK
cp .env.example .env
./scripts/bootstrap.sh
./scripts/verify.sh
sudo ./scripts/install.sh
```

Open `http://<RK3588-IP>:8080`. The service serves the production frontend and API from the same port.

`bootstrap.sh` creates local Python environments, installs the bundled RKNN Lite wheel, builds the frontend, and downloads Piper Amy low. It does not download the large ASR image/model bundle. Configure `VOICE_IMAGE` for a published image, or restore the offline voice bundle before starting the voice demos.

For development without systemd:

```bash
./scripts/start.sh
./scripts/status.sh
./scripts/stop.sh
```

## Configuration

All device-specific values live in `.env`; no `/home/seeed` installation path is required. Important settings include `RECAMERA_IP`, `RECAMERA_RTSP_URL`, `RECAMERA_WS_URL`, `VOICE_IMAGE`, `LLM_SUMMARY_URL`, `PIPER_SINK`, and `GO2RTC_BIN`.

Piper uses `en_US-amy-low` with `PIPER_LENGTH_SCALE=1.12`. A larger length scale speaks more slowly. The expected ES8311 PulseAudio sink is shown in `.env.example` and can be changed for another audio device.

## Models and large assets

The two RKNN demo models and sample video are versioned because each file is below GitHub's file limit. Piper is downloaded from its official release and Hugging Face repository with SHA-256 checks. The ASR runtime and RKLLM image are intentionally external because the complete offline bundle is several gigabytes.

See [models/README.md](models/README.md) and [models/manifest.json](models/manifest.json) for the exact asset policy.

## Validate

```bash
./scripts/verify.sh
curl -fsS http://127.0.0.1:8080/api/v1/health
curl -fsS http://127.0.0.1:8080/api/v1/hardware/status
```

The verification script runs backend tests, compiles Python sources, builds the Vue frontend, validates shell syntax, checks model files, and reports optional hardware services without resetting USB devices or rebooting the host.

## Documentation

- [Architecture](docs/architecture.md)
- [Hardware setup](docs/hardware-setup.md)
- [Troubleshooting](docs/troubleshooting.md)
- [SenseCraft AI Lab article draft](docs/ai-lab-project.md)
- [GitHub publishing checklist](docs/github-publish.md)

## Known constraints

- RKNN files only run on compatible Rockchip hardware and runtime versions.
- The voice ASR image/model must be supplied separately.
- Browser WebRTC availability depends on go2rtc; MJPEG remains available without it.
- The first demo needs fresh reCamera inventory data for inventory answers, but voice health and transcription can still be inspected while reCamera is offline.

## License

Original integration code in this repository is released under Apache-2.0. Bundled models, sample media, vendor wheels, and upstream services retain their own terms. Review [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) before publishing a public repository.
