# Troubleshooting

## Page opens but a demo remains initializing

```bash
./scripts/status.sh
journalctl -u recomputer-retail-ai.service -n 150 --no-pager
curl -fsS http://127.0.0.1:8080/api/v1/showcase/status
```

The single-camera view may initialize before reCamera finishes booting. Confirm ports 8554 and 9002, then check that inventory snapshots report `initialized=true`.

## Voice text appears but no sound plays

Check the service user's audio session and sink:

```bash
pactl list short sinks
pactl get-default-sink
ls -l /run/user/$(id -u)/pulse/native
curl -fsS http://127.0.0.1:8080/api/v1/inventory-voice/tts/status
```

Set `PIPER_SINK` to the exact sink name and restart the service. A missing desktop session can also make the PulseAudio socket unavailable. Replay uses the backend audio device, not browser audio.

## Voice transcription is unavailable

The Git repository does not contain the multi-gigabyte ASR image and model volume. Confirm that `VOICE_IMAGE` exists locally, the `openvoicestream` container is running, and port 8621 returns health. Use the separately distributed offline bundle when the device has no Internet access.

## WebRTC is unavailable

Install a compatible arm64 go2rtc binary and set `GO2RTC_BIN`. The UI will use MJPEG fallback when the bridge is absent, so camera inventory and detection still operate.

## Four-stream demo fails immediately

Confirm the platform is arm64 RK3588, the bundled RKNN Lite wheel installed successfully, `/dev/dri` and NPU devices are accessible, and the model files are non-empty. Run:

```bash
demos/multi-camera-shelf/.venv/bin/python demos/multi-camera-shelf/tools/probe_rk3588.py
```

Do not install desktop `rknn-toolkit2` into the runtime environment.
