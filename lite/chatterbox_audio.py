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
from typing import Iterable

from .audio import AudioError, wav_info
from .chatterbox_config import ffmpeg_path


def stretch_speech(data: bytes, speed: float, deadline_seconds: float, max_bytes: int) -> bytes:
    if not math.isfinite(speed) or not 0.75 <= speed <= 1.25:
        raise AudioError("INVALID_SPEED")
    wav_info(data, max_bytes=max_bytes, max_seconds=600)
    if speed == 1.0:
        return data
    ffmpeg = ffmpeg_path()
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


# Independent final-output limit: 15 minutes at 24 kHz mono PCM16.
# Per-section/API WAVs remain capped at 600 seconds.
FINAL_MAX_SECONDS = 900
FINAL_MAX_BYTES = FINAL_MAX_SECONDS * 24000 * 2 + 65536


def concatenate(wavs: Iterable[bytes], max_bytes: int = FINAL_MAX_BYTES,
                max_seconds: int = FINAL_MAX_SECONDS) -> bytes:
    """Bounded one-pass PCM copy; no repeated or list-joined audio buffers."""
    if not 0 < max_seconds <= FINAL_MAX_SECONDS or max_bytes > FINAL_MAX_BYTES:
        raise AudioError("INVALID_FINAL_AUDIO_LIMIT")
    total_frames = 0
    out = io.BytesIO()
    with wave.open(out, "wb") as writer:
        writer.setnchannels(1); writer.setsampwidth(2); writer.setframerate(24000)
        for data in wavs:
            info = wav_info(data, max_bytes=min(max_bytes, 100 * 1024 * 1024),
                            max_seconds=600)
            frames = info["frames"]
            total_frames += frames
            if total_frames > max_seconds * 24000 or total_frames * 2 + 65536 > max_bytes:
                raise AudioError("FINAL_AUDIO_LIMIT_EXCEEDED")
            with wave.open(io.BytesIO(data), "rb") as reader:
                # Copy in bounded chunks rather than join all source PCM.
                remaining = frames
                while remaining:
                    count = min(remaining, 24000)
                    pcm = reader.readframes(count)
                    if len(pcm) != count * 2:
                        raise AudioError("TRUNCATED_PCM")
                    writer.writeframesraw(pcm)
                    remaining -= count
    if total_frames == 0:
        raise AudioError("EMPTY_ASSEMBLY")
    final = out.getvalue()
    wav_info(final, max_bytes=max_bytes, max_seconds=max_seconds)
    return final


async def stretch_speech_async(data: bytes, speed: float, deadline_seconds: float,
                               max_bytes: int, temp_parent: Path) -> bytes:
    """Async atempo with deterministic process kill/reap on cancel or timeout."""
    import asyncio
    if not math.isfinite(speed) or not 0.75 <= speed <= 1.25:
        raise AudioError("INVALID_SPEED")
    wav_info(data, max_bytes=max_bytes, max_seconds=600)
    if speed == 1.0:
        return data
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise AudioError("FFMPEG_UNAVAILABLE")
    temp_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="chatterbox-atempo-", dir=temp_parent) as temp:
        source, target = Path(temp) / "in.wav", Path(temp) / "out.wav"
        source.write_bytes(data)
        args = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(source), "-map", "0:a:0", "-vn",
                "-filter:a", f"atempo={speed:.6f}", "-ar", "24000", "-ac", "1",
                "-c:a", "pcm_s16le", "-f", "wav", str(target)]
        proc = await asyncio.create_subprocess_exec(*args,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(proc.wait(), timeout=max(0.001, deadline_seconds))
        except (asyncio.CancelledError, asyncio.TimeoutError) as exc:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            if isinstance(exc, asyncio.TimeoutError):
                raise AudioError("ATEMPO_TIMEOUT") from exc
            raise
        if proc.returncode != 0 or not target.is_file():
            raise AudioError("ATEMPO_FAILED")
        if target.stat().st_size > max_bytes:
            raise AudioError("OUTPUT_TOO_LARGE")
        result = target.read_bytes()
        wav_info(result, max_bytes=max_bytes, max_seconds=600)
        return result
