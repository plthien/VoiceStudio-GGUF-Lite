"""Bounded, deterministic assembly of verified native 24-kHz mono PCM16 WAVs."""
from __future__ import annotations
import io
import wave
from .audio import AudioError, wav_info
from .pause import PausePart, SpeechPart, PauseScript

RATE = 24000
BYTES_PER_FRAME = 2

def assemble_pause_wav(script: PauseScript, segments: list[bytes],
                       *, max_bytes: int = 100*1024*1024,
                       max_seconds: float = 600) -> tuple[bytes,dict]:
    if len(segments) != script.speech_segments:
        raise AudioError("Mismatch between speech spans and verified WAVs")
    pcm: list[bytes] = []
    speech_frames = 0
    speech_index = 0
    frames = 0
    for part in script.parts:
        if isinstance(part, PausePart):
            n = part.milliseconds*24
            frames += n
            pcm.append(b"\x00" * (n*BYTES_PER_FRAME))
        else:
            data=segments[speech_index]
            speech_index+=1
            info=wav_info(data, max_bytes=max_bytes, max_seconds=max_seconds)
            n=info["frames"]
            frames += n
            speech_frames += n
            with wave.open(io.BytesIO(data),"rb") as reader:
                raw=reader.readframes(n)
            if len(raw)!=n*2:
                raise AudioError("Truncated native PCM data")
            pcm.append(raw)
        # Compute before publishing, including a conservative WAV header.
        if frames>int(max_seconds*RATE) or frames*BYTES_PER_FRAME+4096>max_bytes:
            raise AudioError("Assembled WAV exceeds output limits")
    if frames!=speech_frames+script.pause_frames:
        raise AudioError("Pause frame reconciliation failed")
    out=io.BytesIO()
    with wave.open(out,"wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(RATE)
        writer.writeframes(b"".join(pcm))
    combined=out.getvalue()
    verified=wav_info(combined,max_bytes=max_bytes,max_seconds=max_seconds)
    if verified["frames"]!=frames:
        raise AudioError("Assembled WAV frame count changed")
    return combined, {"pause_count":script.pause_count,"pause_frames":script.pause_frames,
        "speech_frames":speech_frames,"audio_frames":frames,
        "speech_segments":script.speech_segments}
