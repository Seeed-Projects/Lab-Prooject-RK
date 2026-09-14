# Hardware setup

## Connections

1. Connect reCamera by USB networking or Ethernet and confirm its configured IP. The default is `192.168.42.1`.
2. Connect ReSpeaker XVF3800 to a USB 3 port. Its ALSA card should include `Array`.
3. Connect headphones or an amplified speaker to the reComputer ES8311 3.5 mm jack.
4. Keep the display session logged in when using PulseAudio/PipeWire output from the system service.

## Checks

```bash
ping -c 1 192.168.42.1
nc -vz 192.168.42.1 8554
nc -vz 192.168.42.1 9002
arecord -l
pactl list short sinks
curl -fsS http://127.0.0.1:8621/health
curl -fsS http://127.0.0.1:8001/health
```

Update `.env` if the camera address, ALSA card name, LLM endpoint, or output sink differs. `PIPER_LENGTH_SCALE=1.12` is the tested speaking speed; larger values slow the voice down.

## reCamera contract

- Detected RTSP: `rtsp://<camera-ip>:8554/detected`
- Product state WebSocket: `ws://<camera-ip>:9002`
- Inventory messages must include initialization state, product presence, and counts.

The voice pipeline can start without reCamera. Inventory questions will display and speak a data-unavailable answer until a fresh initialized snapshot arrives.
