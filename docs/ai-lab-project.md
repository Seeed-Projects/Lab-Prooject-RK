# SenseCraft AI Lab project draft

## Multimodal Retail AI Demo Center on reComputer RK3588

### Overview

This project packages three retail AI experiences into one browser interface running locally on reComputer RK3588. It combines a reCamera, a ReSpeaker XVF3800, RKNN visual inference, local speech recognition, a local language model, and Piper text-to-speech. No cloud API is required during the demonstration.

### What it demonstrates

1. **Single-camera smart shelf:** reCamera provides a detected RTSP stream and live product state. Staff can ask open or suggested questions such as “How many bottles are there?”, “How many types are on the shelf?”, and “Which products are missing?”. The answer is shown as structured data and spoken through the 3.5 mm output.
2. **Multi-camera shelf monitoring:** four independent streams run RKNN detection on the RK3588 NPU and present shelf state and restocking events together.
3. **AI sales conversation analysis:** ReSpeaker captures a conversation, separates speaker turns, produces an English transcript, and generates a local summary.

### Why local inference

Retail environments need predictable latency, privacy, and continued operation when Internet access is limited. The application keeps audio, video, inventory state, and generated answers on the device. The first assistant also prevents hallucinated inventory: the LLM identifies intent while deterministic code calculates every count from a fresh reCamera snapshot.

### Hardware

- reComputer RK3588
- reCamera
- ReSpeaker XVF3800
- Display and ES8311 3.5 mm headphones/speaker

### Software flow

Clone the repository, copy `.env.example` to `.env`, run `scripts/bootstrap.sh`, validate with `scripts/verify.sh`, and install the systemd service. The Vue frontend and FastAPI backend then share port 8080. Large ASR assets are installed through a separate image or offline bundle.

### Suggested video sequence

1. Show the device and browser selector for five seconds.
2. Open Demo 1, remove a bottle, ask two inventory questions, and replay one spoken answer.
3. Switch to Demo 2 and show all four live inference feeds plus one restock alert.
4. Switch to Demo 3, speak a short two-person exchange, then show speaker turns and the generated summary.
5. End on the hardware health panel and repository URL.

### Results

- Backend automated tests: run `scripts/verify.sh` and insert the final count here.
- Frontend production build: verified on the target RK3588.
- English TTS: Piper `en_US-amy-low`, length scale 1.12.
- Inventory answers: rejected when reCamera data is stale or unavailable.

### Repository

Add the public GitHub URL, release version, bill of materials, and final demonstration video before publishing this article.
