"""Bounded RIFF/PCM16 validation and FFmpeg reference normalization."""
from __future__ import annotations
import hashlib
import io
import os
import subprocess
import wave
from pathlib import Path

class AudioError(ValueError):
    pass

def wav_info(data: bytes, *, max_bytes: int = 100 * 1024 * 1024, max_seconds: float = 600) -> dict:
    if len(data) > max_bytes or len(data) < 46 or not data.startswith(b"RIFF") or data[8:12] != b"WAVE":
        raise AudioError("Invalid or oversized RIFF/WAVE output")
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            if w.getcomptype() != "NONE" or w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != 24000:
                raise AudioError("Expected 24 kHz mono PCM16 WAV")
            frames = w.getnframes()
            if frames <= 0 or frames / 24000 > max_seconds:
                raise AudioError("Missing or excessive WAV frames")
            # Parse/decode all declared samples, not just a plausible header.
            audio = w.readframes(frames)
            if len(audio) != frames * 2:
                raise AudioError("Truncated PCM payload")
            return {"frames": frames, "seconds": frames / 24000, "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest()}
    except (wave.Error, EOFError, OSError) as exc:
        raise AudioError("Undecodable WAV") from exc

def wav_file(path: Path, *, max_bytes: int = 100 * 1024 * 1024, max_seconds: float = 600) -> dict:
    if not path.is_file() or path.stat().st_size > max_bytes:
        raise AudioError("No complete WAV output")
    return wav_info(path.read_bytes(), max_bytes=max_bytes, max_seconds=max_seconds)

def convert_reference(source: Path, target: Path, *, ffmpeg: str, max_seconds: float = 60.0) -> dict:
    """Always re-decode even valid WAVs, validating bytes rather than suffix."""
    target.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
           "-y", "-i", str(source), "-map", "0:a:0", "-vn",
           "-t", str(max_seconds + 1), "-ar", "24000", "-ac", "1",
           "-c:a", "pcm_s16le", "-f", "wav", str(target)]
    try:
        p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           timeout=90, check=False)
        if p.returncode != 0:
            raise AudioError("Reference audio cannot be decoded")
        return wav_file(target, max_bytes=int(max_seconds + 2) * 48000 + 65536,
                        max_seconds=max_seconds)
    except subprocess.TimeoutExpired as exc:
        raise AudioError("Reference normalization timed out") from exc
    except OSError as exc:
        raise AudioError("FFmpeg is unavailable") from exc
    finally:
        # On error the caller also removes the staging directory.
        if target.exists():
            try:
                wav_file(target, max_bytes=int(max_seconds + 2) * 48000 + 65536,
                         max_seconds=max_seconds)
            except AudioError:
                target.unlink(missing_ok=True)
