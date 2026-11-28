"""Wav2Vec2 forced-alignment HTTP surface.

Adds two routes without altering any existing contract:

- ``GET /lite/alignment-capability`` — discovery, in the style of the
  other ``/lite/*-capability`` endpoints. Reports truthfully whether the
  torchaudio engine is importable; it never loads the model.
- ``POST /v1/forced-alignment`` — ElevenLabs-shaped forced alignment:
  multipart form fields ``file`` (WAV) and ``text`` (the exact spoken
  transcript), returning ``{"characters": [...], "words": [...],
  "loss": ...}``. This lets a consumer configured for ElevenLabs forced
  alignment (e.g. YouTube AI Studio via its ELEVENLABS_API_BASE_URL
  setting) use this local, free engine instead, with no consumer-side
  code change.

Alignment runs on CPU by default (VOICESTUDIO_ALIGNMENT_DEVICE=cuda opts
into the GPU) and is serialized behind its own lock, independent of the
TTS worker admission gate: it neither blocks nor is blocked by speech
generation.
"""
from __future__ import annotations

import asyncio

from fastapi import File, Form, UploadFile
from fastapi.responses import JSONResponse

from .alignment_engine import (CONTRACT_VERSION, MODEL_BUNDLE, AlignmentError,
                               Wav2Vec2Aligner, engine_available,
                               requested_device)

MAX_TEXT_CHARS = 20000

_STATUS_BY_CODE = {
    "ALIGNMENT_ENGINE_UNAVAILABLE": 503,
    "ALIGNMENT_AUDIO_INVALID": 422,
    "ALIGNMENT_TEXT_INVALID": 422,
    "ALIGNMENT_FAILED": 422,
}


def fail(status: int, code: str) -> JSONResponse:
    return JSONResponse({"error": {"type": "alignment_error", "code": code,
                                   "message": code}}, status_code=status,
                        headers={"Cache-Control": "no-store"})


def get_aligner(app) -> Wav2Vec2Aligner:
    """Return the app-scoped aligner, creating it (unloaded) on demand.

    Tests may pre-seed ``app.state.alignment_aligner`` with a stub.
    """
    aligner = getattr(app.state, "alignment_aligner", None)
    if aligner is None:
        aligner = Wav2Vec2Aligner()
        app.state.alignment_aligner = aligner
    return aligner


def mount_alignment(app) -> None:
    @app.get("/lite/alignment-capability")
    def alignment_capability():
        aligner = get_aligner(app)
        return {
            "contract_version": CONTRACT_VERSION,
            "engine": "wav2vec2",
            "model_bundle": MODEL_BUNDLE,
            "provider_route": "POST /v1/forced-alignment",
            "response_shape": "elevenlabs_forced_alignment_compatible",
            "engine_available": engine_available(),
            "device_requested": requested_device(),
            "device": aligner.device if aligner.loaded else None,
            "model_loaded": aligner.loaded,
            "max_text_characters": MAX_TEXT_CHARS,
        }

    @app.post("/v1/forced-alignment")
    async def forced_alignment(file: UploadFile = File(...),
                               text: str = Form(...)):
        if not text or not text.strip() or len(text) > MAX_TEXT_CHARS:
            return fail(422, "ALIGNMENT_TEXT_INVALID")
        wav_bytes = await file.read()
        if not wav_bytes:
            return fail(422, "ALIGNMENT_AUDIO_INVALID")
        aligner = get_aligner(app)
        lock = getattr(app.state, "alignment_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            app.state.alignment_lock = lock
        async with lock:
            try:
                payload = await asyncio.to_thread(aligner.align, wav_bytes, text)
            except AlignmentError as exc:
                return fail(_STATUS_BY_CODE.get(exc.code, 500), exc.code)
            except Exception:
                return fail(500, "ALIGNMENT_INTERNAL")
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})
