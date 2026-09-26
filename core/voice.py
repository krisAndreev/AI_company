"""
Voiceover (text-to-speech) for videos.

  piper       local neural TTS on the CPU (free, offline; voice model in models/voices)
  elevenlabs  ElevenLabs API (API key from env ELEVENLABS_API_KEY; paid per character)

Every clip is written as 16-bit mono WAV so code can measure it and lay it on the
video timeline. Model text is spoken as-is (after control-character cleanup); it is
never interpreted as markup or commands.
"""

import os
import re
import subprocess
import threading
import wave
from pathlib import Path
from typing import Literal

from pydantic import Field

from core import engines, net
from core.net import ToolError
from core.schemas import StrictModel

SAMPLE_RATE = 22050


class VoiceSettings(StrictModel):
    engine: Literal["piper", "elevenlabs", "none"] = "piper"
    piper_voice: str = "models/voices/en_US-lessac-medium.onnx"
    piper_length_scale: float = Field(default=1.0, ge=0.6, le=1.6, description=">1 = slower")
    elevenlabs_voice_id: str = "21m00Tcm4TlvDq8ikWAM"
    elevenlabs_model: str = "eleven_multilingual_v2"
    elevenlabs_api_key_env: str = "ELEVENLABS_API_KEY"
    elevenlabs_cost_per_1k_chars_eur: float = Field(default=0.25, ge=0)


_voices: dict[str, object] = {}
_voice_lock = threading.Lock()


def clean_speech(text: str) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = re.sub(r"[#*_`<>\[\]{}|]", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:1200]


def availability(s: VoiceSettings) -> tuple[bool, str]:
    if s.engine == "none":
        return False, "voiceover disabled"
    if s.engine == "elevenlabs":
        return (True, "ok") if os.environ.get(s.elevenlabs_api_key_env) else \
            (False, f"environment variable {s.elevenlabs_api_key_env} not set")
    if not engines.resolve(s.piper_voice).is_file():
        return False, f"voice model missing: {s.piper_voice}"
    try:
        engines.enable_runtime_dlls()
        import piper  # noqa: F401
    except Exception as e:
        return False, f"piper not usable: {type(e).__name__}"
    return True, "ok"


def estimate_cost(s: VoiceSettings, chars: int) -> float:
    return s.elevenlabs_cost_per_1k_chars_eur * chars / 1000 if s.engine == "elevenlabs" else 0.0


def synthesize(text: str, s: VoiceSettings, out_wav: Path, allowed_hosts: list[str]) -> float:
    """Speak text into out_wav. Returns the clip length in seconds. Raises ToolError."""
    text = clean_speech(text)
    if not text:
        raise ToolError("nothing to say")
    ok, why = availability(s)
    if not ok:
        raise ToolError(f"voiceover unavailable: {why}")
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    if s.engine == "piper":
        _piper(text, s, out_wav)
    else:
        _elevenlabs(text, s, out_wav, allowed_hosts)
    return wav_seconds(out_wav)


def _piper(text: str, s: VoiceSettings, out_wav: Path) -> None:
    engines.enable_runtime_dlls()
    from piper import PiperVoice, SynthesisConfig
    path = str(engines.resolve(s.piper_voice))
    with _voice_lock:
        voice = _voices.get(path)
        if voice is None:
            voice = _voices[path] = PiperVoice.load(path)
        with wave.open(str(out_wav), "wb") as wf:
            voice.synthesize_wav(text, wf, syn_config=SynthesisConfig(
                length_scale=s.piper_length_scale))
    to_standard_wav(out_wav)


def _elevenlabs(text: str, s: VoiceSettings, out_wav: Path, allowed_hosts: list[str]) -> None:
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{s.elevenlabs_voice_id}"
    mp3 = net.request("POST", url, allowed_hosts, params={"output_format": "mp3_44100_128"},
                      json_body={"text": text, "model_id": s.elevenlabs_model},
                      headers={"xi-api-key": os.environ.get(s.elevenlabs_api_key_env, ""),
                               "Accept": "audio/mpeg"}, expect="bytes", timeout=120)
    decode_to_wav(mp3, out_wav)


def decode_to_wav(data: bytes, out_wav: Path) -> None:
    ffmpeg = engines.ffmpeg_exe()
    if not ffmpeg:
        raise ToolError("ffmpeg not available")
    proc = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", "pipe:0", "-ac", "1",
                           "-ar", str(SAMPLE_RATE), "-sample_fmt", "s16", str(out_wav)],
                          input=data, capture_output=True, timeout=120,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc.returncode != 0:
        raise ToolError("could not decode speech audio")


def to_standard_wav(path: Path) -> None:
    """Resample to SAMPLE_RATE mono 16-bit if the engine produced something else."""
    with wave.open(str(path), "rb") as r:
        if (r.getframerate(), r.getnchannels(), r.getsampwidth()) == (SAMPLE_RATE, 1, 2):
            return
    decode_to_wav(path.read_bytes(), path)


def wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as r:
        return r.getnframes() / float(r.getframerate())


def build_track(clips: list[tuple[float, Path | None]], total_seconds: float, out_wav: Path) -> None:
    """Place clips at start times (seconds) on a silent track of total_seconds."""
    n_total = int(total_seconds * SAMPLE_RATE)
    track = bytearray(n_total * 2)
    for start, clip in clips:
        if clip is None:
            continue
        with wave.open(str(clip), "rb") as r:
            frames = r.readframes(r.getnframes())
        offset = int(start * SAMPLE_RATE) * 2
        end = min(len(track), offset + len(frames))
        track[offset:end] = frames[: end - offset]
    with wave.open(str(out_wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(bytes(track))


def silence_wav(seconds: float, out_wav: Path) -> None:
    build_track([], seconds, out_wav)
