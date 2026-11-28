"""Protocol-correct deterministic Nano HTTP responses for offline tests."""
from __future__ import annotations

import hashlib

from lite.audio import wav_info
from lite.chatterbox_config import MODEL_REVISION
from lite.chatterbox_contract import build_receipt, frame_seconds, speed_ascii
from lite.chatterbox_segmentation import POLICY_VERSION


def nano_response_headers(*, profile: dict, binding: str, text: str,
                          wav: bytes, seed: int | None = 42,
                          speed: float = 1.0) -> dict[str, str]:
    """Build the same authoritative envelope emitted by the real API."""
    audio = wav_info(wav)
    chunk_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    receipt, _raw, token, proof = build_receipt(
        profile=profile, reference_binding=binding, input_text=text,
        seed=seed, speed=speed, wav=wav,
        info={
            "audio_frames": audio["frames"],
            "speech_frames": audio["frames"],
            "pause_frames": 0,
            "pause_count": 0,
            "speech_segments": 1,
            "native_speech_segments": 1,
            "chunk_text_sha256": [chunk_hash],
            "model": "chatterbox-nano",
            "backend": "CUDA0",
            "model_revision": MODEL_REVISION,
        },
    )
    duration = frame_seconds(receipt["audio_frames"])
    return {
        "Content-Type": "audio/wav",
        "X-TTS-Execution-Receipt": token,
        "X-TTS-Execution-Proof": proof,
        "X-TTS-Engine": receipt["model"],
        "X-TTS-Backend": receipt["backend"],
        "X-TTS-Model-Revision": receipt["model_revision"],
        "X-TTS-Voice-Version": str(receipt["voice_version"]),
        "X-TTS-Requested-Speed": speed_ascii(speed),
        "X-TTS-Effective-Speed": speed_ascii(speed),
        "X-TTS-Audio-S": duration,
        "X-Lite-Audio-S": duration,
        "X-Lite-Speech-S": duration,
        "X-Lite-Pause-S": frame_seconds(0),
        "X-Lite-Pause-Count": "0",
        "X-Lite-Speech-Segments": "1",
        "X-TTS-Native-Chunks": "1",
        "X-TTS-Segmentation-Policy": POLICY_VERSION,
    }
