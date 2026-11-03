"""FFmpeg speech-only pitch-preserving time-stretch and verified PCM frames."""
from __future__ import annotations
import io
import math
import os
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

from .audio import AudioError, wav_info


def stretch_speech(data: bytes, speed: float, deadline_seconds: float, max_bytes: int) -> bytes:
    if not math.isfinite(speed) or not 0.75 <= speed <= 1.25:
        raise AudioError("INVALID_SPEED")
    wav_info(data, max_bytes=max_bytes, max_seconds=600)
    if speed == 1.0:
        return data
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise AudioError("FFMPEG_UNAVAILABLE")
    base = os.environ.get("CHATTERBOX_ROOT")
    temp_parent = Path(base)/"cache"/"tmp" if base else None
    if temp_parent:
        temp_parent.mkdir(parents=True, exist_ok=True)
    # FFmpeg pipe WAV uses unbounded RIFF data lengths; use securely isolated files
    # on D: to produce a finalized canonical header, then delete both files.
    with tempfile.TemporaryDirectory(prefix="chatterbox-atempo-", dir=temp_parent) as temp:
        source = Path(temp)/"in.wav"
        target = Path(temp)/"out.wav"
        source.write_bytes(data)
        args = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                "-y", "-i", str(source), "-map", "0:a:0", "-vn",
                "-filter:a", f"atempo={speed:.6f}", "-ar", "24000", "-ac", "1",
                "-c:a", "pcm_s16le", "-f", "wav", str(target)]
        try:
            proc = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                  timeout=max(0.1, deadline_seconds), check=False)
        except subprocess.TimeoutExpired as exc:
            raise AudioError("ATEMPO_TIMEOUT") from exc
        if proc.returncode != 0 or not target.is_file():
            raise AudioError("ATEMPO_FAILED")
        if target.stat().st_size > max_bytes:
            raise AudioError("OUTPUT_TOO_LARGE")
        result = target.read_bytes()
        wav_info(result, max_bytes=max_bytes, max_seconds=600)
        return result


def concatenate(wavs: list[bytes], max_bytes: int = 100 * 1024 * 1024) -> bytes:
    total = 0
    payloads = []
    for data in wavs:
        info = wav_info(data, max_bytes=max_bytes, max_seconds=600)
        with wave.open(io.BytesIO(data), "rb") as w:
            pcm = w.readframes(info["frames"])
        total += len(pcm)
        if total + 4096 > max_bytes:
            raise AudioError("OUTPUT_TOO_LARGE")
        payloads.append(pcm)
    if total == 0:
        raise AudioError("EMPTY_ASSEMBLY")
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
        w.writeframes(b"".join(payloads))
    data = out.getvalue()
    wav_info(data, max_bytes=max_bytes, max_seconds=600)
    return data
