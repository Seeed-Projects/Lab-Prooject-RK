# Runtime assets

This directory is the default location for downloaded runtime assets.

```text
models/
  piper/
    piper
    voices/en_US-amy-low.onnx
    voices/en_US-amy-low.onnx.json
  go2rtc/                 # optional
```

Run `../scripts/download-assets.sh` to install Piper and the Amy low voice. Downloads are pinned and validated against `manifest.json`. Downloaded files are ignored by Git.

The multi-camera RKNN models are stored under `demos/multi-camera-shelf/models/rknn`. The ASR model volume and Docker image are not included because the offline bundle is about 4.4 GB. Set `VOICE_IMAGE` to a published compatible image or restore a separately distributed offline bundle.
