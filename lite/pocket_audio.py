"""Pocket-only bounded PCM integrity and speed/assembly primitives.

No mastering and no model inference take place here. Accept only canonical
24 kHz mono PCM16. Silence is allowed only for explicit pause spans.
"""
from __future__ import annotations
import hashlib
import io
import math
import struct
import subprocess
import tempfile
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path
import sys

from .pocket_registry import (
    SAMPLE_RATE, MAX_OUTPUT_BYTES, MAX_OUTPUT_SECONDS, SPEED_MIN, SPEED_MAX, PocketError,
)

MAX_FRAMES = SAMPLE_RATE * MAX_OUTPUT_SECONDS
MAX_CHUNK_SECONDS = 120

@dataclass(frozen=True)
class AudioInfo:
    frames: int
    sample_rate: int
    byte_count: int
    sha256: str

    @property
    def seconds(self):
        return self.frames / self.sample_rate

def _samples(pcm: bytes):
    values = array("h")
    values.frombytes(pcm)
    if sys.byteorder != "little":
        values.byteswap()
    return values

def encode_pcm(pcm: bytes) -> bytes:
    if not pcm or len(pcm) % 2:
        raise PocketError("AUDIO_INTEGRITY_FAILURE")
    if len(pcm) // 2 > MAX_FRAMES or len(pcm) + 44 > MAX_OUTPUT_BYTES:
        raise PocketError("OUTPUT_LIMIT_EXCEEDED")
    target = io.BytesIO()
    with wave.open(target, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(SAMPLE_RATE)
        writer.writeframes(pcm)
    return target.getvalue()

def inspect_wav(data: bytes, *, max_seconds: int = MAX_OUTPUT_SECONDS):
    if len(data) > MAX_OUTPUT_BYTES:
        raise PocketError("OUTPUT_LIMIT_EXCEEDED")
    # RIFF size must agree with the delivered bytes, with no trailing payload.
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise PocketError("AUDIO_INTEGRITY_FAILURE")
    if struct.unpack_from("<I", data, 4)[0] + 8 != len(data):
        raise PocketError("AUDIO_INTEGRITY_FAILURE")
    offset = 12
    pcm_size = None
    fmt_seen = False
    while offset < len(data):
        if len(data) - offset < 8:
            raise PocketError("AUDIO_INTEGRITY_FAILURE")
        kind, size = struct.unpack_from("<4sI", data, offset)
        start = offset + 8
        stop = start + size
        offset = stop + (size % 2)
        if offset > len(data):
            raise PocketError("AUDIO_INTEGRITY_FAILURE")
        if kind == b"fmt ":
            if fmt_seen or pcm_size is not None or size < 16:
                raise PocketError("AUDIO_INTEGRITY_FAILURE")
            if struct.unpack_from("<HHIIHH", data, start) != (1, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16):
                raise PocketError("AUDIO_INTEGRITY_FAILURE")
            fmt_seen = True
        elif kind == b"data":
            if not fmt_seen or pcm_size is not None or size % 2:
                raise PocketError("AUDIO_INTEGRITY_FAILURE")
            pcm_size = size
    if pcm_size is None:
        raise PocketError("AUDIO_INTEGRITY_FAILURE")
    try:
        with wave.open(io.BytesIO(data), "rb") as reader:
            frames = reader.getnframes()
            if (reader.getnchannels(), reader.getsampwidth(), reader.getframerate(),
                    reader.getcomptype()) != (1, 2, SAMPLE_RATE, "NONE"):
                raise PocketError("AUDIO_INTEGRITY_FAILURE")
            if frames > min(max_seconds * SAMPLE_RATE, MAX_FRAMES):
                raise PocketError("OUTPUT_LIMIT_EXCEEDED")
            pcm = reader.readframes(frames)
            if frames <= 0 or len(pcm) != frames * 2 or len(pcm) != pcm_size:
                raise PocketError("AUDIO_INTEGRITY_FAILURE")
    except (wave.Error, EOFError, struct.error, OverflowError) as exc:
        raise PocketError("AUDIO_INTEGRITY_FAILURE") from exc
    values = _samples(pcm)
    if not any(values):
        raise PocketError("AUDIO_INTEGRITY_FAILURE")
    # Reject more than 0.1% saturated samples (documented application threshold).
    if sum(v in (-32768, 32767) for v in values) * 1000 > frames:
        raise PocketError("AUDIO_INTEGRITY_FAILURE")
    return AudioInfo(frames, SAMPLE_RATE, len(data), hashlib.sha256(data).hexdigest()), pcm

def apply_speed(data: bytes, speed: float, ffmpeg: str | None = None):
    if isinstance(speed, bool) or not isinstance(speed, (float, int)) or not math.isfinite(speed) or not SPEED_MIN <= speed <= SPEED_MAX:
        raise PocketError("SPEED_OUT_OF_RANGE")
    inspect_wav(data, max_seconds=MAX_CHUNK_SECONDS)
    if speed == 1:
        return data
    if not ffmpeg:
        raise PocketError("FFMPEG_UNAVAILABLE")
    with tempfile.TemporaryDirectory(prefix="pocket-atempo-") as directory:
        source = Path(directory) / "input.wav"
        target = Path(directory) / "output.wav"
        source.write_bytes(data)
        try:
            subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                            "-i", str(source), "-af", f"atempo={speed:.9g}",
                            "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le",
                            "-fs", str(MAX_OUTPUT_BYTES), "-y", str(target)],
                           check=True, timeout=60, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            if target.stat().st_size > MAX_OUTPUT_BYTES:
                raise PocketError("OUTPUT_LIMIT_EXCEEDED")
            result = target.read_bytes()
        except (OSError, subprocess.SubprocessError) as exc:
            raise PocketError("AUDIO_INTEGRITY_FAILURE") from exc
    inspect_wav(result, max_seconds=math.ceil(MAX_CHUNK_SECONDS / SPEED_MIN))
    return result

class Assembly:
    def __init__(self):
        self._pcm = bytearray()
        self.pause_frames = 0
        self.speech_frames = 0

    def _check(self, frames: int):
        total = len(self._pcm) // 2 + frames
        if total > MAX_FRAMES or total * 2 + 44 > MAX_OUTPUT_BYTES:
            raise PocketError("OUTPUT_LIMIT_EXCEEDED")

    def speech(self, data: bytes):
        info, pcm = inspect_wav(data, max_seconds=math.ceil(MAX_CHUNK_SECONDS / SPEED_MIN))
        self._check(info.frames)
        self._pcm.extend(pcm)
        self.speech_frames += info.frames

    def pause(self, milliseconds: int):
        if type(milliseconds) is not int or not 1 <= milliseconds <= 10000:
            raise PocketError("INVALID_PAUSE_MARKER")
        frames = milliseconds * SAMPLE_RATE // 1000
        self._check(frames)
        self._pcm.extend(b"\0" * (frames * 2))
        self.pause_frames += frames

    def finish(self):
        if not self.speech_frames:
            raise PocketError("AUDIO_INTEGRITY_FAILURE")
        data = encode_pcm(bytes(self._pcm))
        info, _ = inspect_wav(data)
        if info.frames != self.speech_frames + self.pause_frames:
            raise PocketError("AUDIO_INTEGRITY_FAILURE")
        return data, info
