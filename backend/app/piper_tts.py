from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from .paths import MODEL_ROOT


DEFAULT_PIPER_BIN = str(MODEL_ROOT / "piper" / "piper")
DEFAULT_PIPER_MODEL = str(MODEL_ROOT / "piper" / "voices" / "en_US-amy-low.onnx")
DEFAULT_AUDIO_SINK = "alsa_output.platform-es8311-sound.stereo-fallback"


class PiperTTSUnavailable(RuntimeError):
    """Raised when the configured local TTS runtime cannot be used."""


class PiperTTS:
    def __init__(
        self,
        *,
        piper_bin: str | None = None,
        model_path: str | None = None,
        length_scale: float | None = None,
        sink: str | None = None,
        player_bin: str | None = None,
        max_text_length: int = 800,
    ) -> None:
        self.piper_bin = Path(piper_bin or os.getenv("PIPER_BIN", DEFAULT_PIPER_BIN))
        self.model_path = Path(model_path or os.getenv("PIPER_MODEL", DEFAULT_PIPER_MODEL))
        self.length_scale = float(
            length_scale if length_scale is not None else os.getenv("PIPER_LENGTH_SCALE", "1.12")
        )
        self.sink = sink or os.getenv("PIPER_SINK", DEFAULT_AUDIO_SINK)
        configured_player = player_bin or os.getenv("PIPER_PLAYER_BIN", "paplay")
        self.player_bin = Path(shutil.which(configured_player) or configured_player)
        self.audio_runtime_dir = Path(os.getenv("PIPER_XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
        self.pulse_socket = self.audio_runtime_dir / "pulse" / "native"
        self.pulse_server = os.getenv("PIPER_PULSE_SERVER", f"unix:{self.pulse_socket}")
        self.max_text_length = max_text_length
        self._lock = asyncio.Lock()
        self._active_process: asyncio.subprocess.Process | None = None
        self._speaking = False
        self._last_error: str | None = None
        self._last_started_at: float | None = None
        self._last_completed_at: float | None = None

    def _configuration_error(self) -> str | None:
        if not self.piper_bin.is_file() or not os.access(self.piper_bin, os.X_OK):
            return f"Piper executable is unavailable: {self.piper_bin}"
        if not self.model_path.is_file():
            return f"Piper voice model is unavailable: {self.model_path}"
        if not self.player_bin.is_file() or not os.access(self.player_bin, os.X_OK):
            return f"Audio player is unavailable: {self.player_bin}"
        if self.length_scale <= 0:
            return "PIPER_LENGTH_SCALE must be greater than zero"
        return None

    def status(self) -> dict[str, Any]:
        configuration_error = self._configuration_error()
        return {
            "configured": configuration_error is None,
            "engine": "piper",
            "voice": self.model_path.stem,
            "model": str(self.model_path),
            "sink": self.sink,
            "audio_socket_ready": self.pulse_socket.exists(),
            "length_scale": self.length_scale,
            "speaking": self._speaking,
            "last_error": self._last_error or configuration_error,
            "last_started_at": self._last_started_at,
            "last_completed_at": self._last_completed_at,
        }

    async def speak(self, text: str) -> dict[str, Any]:
        normalized = " ".join(str(text or "").split())
        if not normalized:
            raise ValueError("text must not be empty")
        if len(normalized) > self.max_text_length:
            raise ValueError(f"text must be at most {self.max_text_length} characters")

        configuration_error = self._configuration_error()
        if configuration_error:
            self._last_error = configuration_error
            raise PiperTTSUnavailable(configuration_error)

        async with self._lock:
            self._speaking = True
            self._last_started_at = time.time()
            self._last_error = None
            wav_path: Path | None = None
            input_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(prefix="inventory-amy-", suffix=".wav", delete=False) as wav:
                    wav_path = Path(wav.name)
                with tempfile.NamedTemporaryFile(prefix="inventory-amy-", suffix=".txt", delete=False) as source:
                    source.write((normalized + "\n").encode("utf-8"))
                    input_path = Path(source.name)

                # Piper's ARM CLI remains in streaming-input mode when stdin is
                # an asyncio pipe. A finite file descriptor gives it an
                # immediate EOF after one utterance and lets it exit cleanly.
                with input_path.open("rb") as source:
                    piper = await asyncio.create_subprocess_exec(
                        str(self.piper_bin),
                        "--quiet",
                        "--model",
                        str(self.model_path),
                        "--length_scale",
                        str(self.length_scale),
                        "--output_file",
                        str(wav_path),
                        stdin=source,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )
                self._active_process = piper
                try:
                    _, piper_stderr = await asyncio.wait_for(piper.communicate(), timeout=30)
                except asyncio.TimeoutError as exc:
                    piper.kill()
                    await piper.wait()
                    raise RuntimeError("Piper synthesis timed out") from exc
                if piper.returncode != 0:
                    detail = piper_stderr.decode("utf-8", errors="replace").strip()
                    raise RuntimeError(f"Piper synthesis failed{': ' + detail if detail else ''}")
                if not wav_path.is_file() or wav_path.stat().st_size <= 44:
                    raise RuntimeError("Piper produced no playable audio")

                player_args = [str(self.player_bin)]
                if self.sink:
                    player_args.append(f"--device={self.sink}")
                player_args.append(str(wav_path))
                player_env = os.environ.copy()
                player_env["XDG_RUNTIME_DIR"] = str(self.audio_runtime_dir)
                player_env["PULSE_SERVER"] = self.pulse_server
                player = await asyncio.create_subprocess_exec(
                    *player_args,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    env=player_env,
                )
                self._active_process = player
                try:
                    _, player_stderr = await asyncio.wait_for(player.communicate(), timeout=90)
                except asyncio.TimeoutError as exc:
                    player.kill()
                    await player.wait()
                    raise RuntimeError("Audio playback timed out") from exc
                if player.returncode != 0:
                    detail = player_stderr.decode("utf-8", errors="replace").strip()
                    raise RuntimeError(f"Audio playback failed{': ' + detail if detail else ''}")

                self._last_completed_at = time.time()
                self._speaking = False
                return self.status() | {"ok": True}
            except Exception as exc:
                self._last_error = str(exc)
                raise
            finally:
                self._active_process = None
                self._speaking = False
                if wav_path is not None:
                    with suppress(FileNotFoundError):
                        wav_path.unlink()
                if input_path is not None:
                    with suppress(FileNotFoundError):
                        input_path.unlink()

    async def stop(self) -> None:
        process = self._active_process
        if process is not None and process.returncode is None:
            process.terminate()
            with suppress(asyncio.TimeoutError, ProcessLookupError):
                await asyncio.wait_for(process.wait(), timeout=3)
            if process.returncode is None:
                process.kill()
                await process.wait()
        self._active_process = None
        self._speaking = False
