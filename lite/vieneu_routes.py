"""Independent VieNeu HTTP surface. Chatterbox nano_lite_api_v1 is untouched."""
from __future__ import annotations
import asyncio
import base64
import contextlib
import hmac
import json
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, field_validator

from .audio import AudioError, convert_reference, wav_info
from .chatterbox_config import ffmpeg_path
from .pause import PauseError
from .vieneu_segmentation import build_job
from .vieneu_config import (CONTRACT, MODEL_REPO, MODEL_REVISION,
                            SDK_REVISION, SDK_VERSION, PROVIDER, VieNeuError, readiness)
from .vieneu_runtime import VieNeuRuntime, MAX_UPLOAD_BYTES

def fail(status: int, code: str) -> JSONResponse:
    return JSONResponse({"error": {"type": "vieneu_error", "code": code,
                                   "message": code}}, status_code=status,
                        headers={"Cache-Control": "no-store"})

class VieNeuSpeech(BaseModel):
    model: str = PROVIDER
    language: str = "vi"
    voice: str = Field(min_length=1, max_length=160)
    input: str = Field(min_length=1, max_length=24000)
    response_format: str = "wav"
    speed: float = Field(default=1.0, ge=0.75, le=1.25, allow_inf_nan=False)
    steps: int = Field(default=16, ge=4, le=32)
    cfg: float = Field(default=3.0, ge=0.0, le=6.0, allow_inf_nan=False)
    sway: float = Field(default=0.0, ge=-1.0, le=1.0, allow_inf_nan=False)
    seed: int | None = Field(default=None, ge=0, le=2147483647)
    @field_validator("model")
    @classmethod
    def model_valid(cls, value):
        if value != PROVIDER: raise ValueError("INVALID_MODEL")
        return value
    @field_validator("language")
    @classmethod
    def language_valid(cls, value):
        if value != "vi": raise ValueError("VIETNAMESE_ONLY")
        return value
    @field_validator("response_format")
    @classmethod
    def wav_valid(cls, value):
        if value != "wav": raise ValueError("INVALID_FORMAT")
        return value

class Rename(BaseModel):
    name: str = Field(min_length=1, max_length=128)

class Cancel(BaseModel):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,128}$")
    cancel_token: str = Field(min_length=24, max_length=256)

def _status(exc: VieNeuError) -> int:
    return (404 if exc.code == "PROFILE_NOT_FOUND" else
            429 if exc.code == "WORKER_BUSY" else
            504 if exc.code == "WORKER_TIMEOUT" else
            503 if exc.code in ("ENV_NOT_INSTALLED", "MODEL_FILES_MISSING",
               "MODEL_HASH_MISMATCH", "MODEL_MANIFEST_INVALID", "MODEL_MANIFEST_UNAVAILABLE",
               "WORKER_CRASHED", "VIENEU_WORKER_FAILED", "INVALID_CPU_ONNX_PROVIDER",
               "MODEL_LOAD_FAILED", "PRESET_LOAD_FAILED", "CLONE_ENCODING_FAILED",
               "SYNTHESIS_FAILED", "PHONEMIZER_FAILED", "AUDIO_ENCODING_FAILED",
               "SEGMENT_STORAGE_FAILED") else
            409 if exc.code in ("PROFILE_INTEGRITY_FAILED", "REFERENCE_CHANGED", "REFERENCE_INVALID") else 422)

async def _reference_audio(runtime: VieNeuRuntime, upload: UploadFile) -> bytes:
    if not upload.filename or Path(upload.filename).suffix.lower() not in (".wav", ".mp3"):
        raise VieNeuError("INVALID_AUDIO")
    raw = await upload.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) < 64 or len(raw) > MAX_UPLOAD_BYTES:
        raise VieNeuError("AUDIO_TOO_LARGE")
    binary = shutil.which("ffmpeg") or ffmpeg_path()
    if not binary:
        raise VieNeuError("FFMPEG_UNAVAILABLE")
    staging = runtime.root / "cache" / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    def convert():
        with tempfile.TemporaryDirectory(dir=staging) as temp:
            source = Path(temp) / ("source" + Path(upload.filename).suffix.lower())
            target = Path(temp) / "normalized.wav"
            source.write_bytes(raw)
            info = convert_reference(source, target, ffmpeg=binary, max_seconds=30)
            if not 3 <= info["seconds"] <= 30:
                raise VieNeuError("INVALID_AUDIO_DURATION")
            return target.read_bytes()
    try:
        return await asyncio.to_thread(convert)
    except (AudioError, OSError) as exc:
        raise VieNeuError("INVALID_AUDIO") from exc

def mount_vieneu(app):
    runtime = VieNeuRuntime()
    app.state.vieneu = runtime
    app.state.vieneu_active = None
    router = APIRouter()

    @router.get("/lite/providers")
    def providers():
        return [{"id": "chatterbox-nano", "language": "en", "backend": "CUDA0",
                 "default_endpoint": "/v1/audio/speech"},
                {"id": PROVIDER, "language": "vi", "backend": "CPUExecutionProvider",
                 "default_endpoint": "/vieneu/v1/audio/speech"}]

    @router.get("/vieneu/capability")
    def capability():
        return {"provider": PROVIDER, "model": PROVIDER, "contract": CONTRACT,
                "backend": "CPUExecutionProvider", "language": "vi", "sample_rate": 24000,
                "channels": 1, "sample_format": "PCM16", "response_format": "wav",
                "model_revision": MODEL_REVISION, "sdk_version": SDK_VERSION,
                "sdk_revision": SDK_REVISION, "speed_min": 0.75, "speed_max": 1.25,
                "default_speed": 1.0, "default_steps": 16, "default_cfg": 3.0,
                "fast_steps": 8, "fast_sway": -1.0, "max_input_chars": 24000,
                "supports_cloning": True, "supports_preset": True,
                "supports_cancel": True, "supports_inline_pause": True}

    @router.get("/vieneu/readiness")
    async def ready():
        state = await asyncio.to_thread(readiness)
        ffmpeg_ready = bool(shutil.which("ffmpeg") or ffmpeg_path())
        state["cloning_ready"] = state["ready"] and ffmpeg_ready
        if not ffmpeg_ready:
            state["issues"] = [*state["issues"], "FFMPEG_UNAVAILABLE"]
        return {**state, "runtime": runtime.status()}

    @router.get("/vieneu/worker/status")
    def worker_status():
        return runtime.status()

    @router.post("/vieneu/worker/unload")
    async def unload():
        try:
            return await runtime.unload()
        except VieNeuError as exc:
            return fail(_status(exc), exc.code)

    @router.post("/vieneu/worker/cancel")
    async def cancel(body: Cancel):
        active = app.state.vieneu_active
        if (not active or body.request_id != active["request_id"] or
            not hmac.compare_digest(body.cancel_token, active["cancel_token"])):
            return fail(409, "NO_ACTIVE_REQUEST")
        active["task"].cancel()
        with contextlib.suppress(BaseException):
            await active["task"]
        return {"cancelled": True, "request_id": body.request_id}

    @router.get("/vieneu/rights")
    def rights():
        return {"model_license": "Apache-2.0", "sdk_license": "Apache-2.0",
                "model_repo": MODEL_REPO, "model_revision": MODEL_REVISION,
                "source_repo": "pnnbao97/VieNeu-TTS", "source_revision": SDK_REVISION,
                "voice_consent_required": True, "local_only": True}

    @router.get("/vieneu/presets")
    async def presets():
        try:
            return await runtime.presets()
        except VieNeuError as exc:
            return fail(_status(exc), exc.code)

    @router.get("/vieneu/profiles")
    def list_profiles():
        return runtime.store.list()

    @router.get("/vieneu/profiles/{voice_id}")
    def get_profile(voice_id: str):
        try:
            return runtime.store.verify(voice_id)["profile"]
        except VieNeuError as exc:
            return fail(_status(exc), exc.code)

    @router.get("/vieneu/profiles/{voice_id}/integrity")
    def integrity(voice_id: str):
        try:
            obj = runtime.store.verify(voice_id)
            return {"verified": True, "provider": PROVIDER, "voice_id": voice_id,
                    "voice_version": obj["profile"]["voice_version"],
                    "conditioning_sha256": obj["profile"]["conditioning_sha256"],
                    "reference_sha256": obj["profile"]["reference_sha256"]}
        except VieNeuError as exc:
            return fail(_status(exc), exc.code)

    @router.get("/vieneu/profiles/{voice_id}/reference")
    def reference(voice_id: str):
        try:
            blob = runtime.store.verify(voice_id)["reference"].read_bytes()
            wav_info(blob, max_seconds=30)
            return Response(blob, media_type="audio/wav", headers={"Cache-Control": "no-store"})
        except VieNeuError as exc:
            return fail(_status(exc), exc.code)
        except AudioError:
            return fail(409, "REFERENCE_INVALID")

    @router.post("/vieneu/profiles")
    async def create_profile(name: str = Form(...), consent: str = Form(...),
                             ref_audio: UploadFile = File(...)):
        if consent != "confirmed":
            return fail(403, "VOICE_CONSENT_REQUIRED")
        try:
            normalized = await _reference_audio(runtime, ref_audio)
            return await runtime.enroll(normalized, name=name)
        except VieNeuError as exc:
            return fail(_status(exc), exc.code)

    @router.put("/vieneu/profiles/{voice_id}")
    async def update_profile(voice_id: str, body: Rename):
        async with runtime.lock:
            try:
                return runtime.store.rename(voice_id, body.name)
            except VieNeuError as exc:
                return fail(_status(exc), exc.code)

    @router.put("/vieneu/profiles/{voice_id}/reference")
    async def replace_profile(voice_id: str, consent: str = Form(...),
                              ref_audio: UploadFile = File(...)):
        if consent != "confirmed":
            return fail(403, "VOICE_CONSENT_REQUIRED")
        try:
            old = runtime.store.verify(voice_id)["profile"]
            normalized = await _reference_audio(runtime, ref_audio)
            return await runtime.enroll(normalized, name=old["name"], voice_id=voice_id)
        except VieNeuError as exc:
            return fail(_status(exc), exc.code)

    @router.delete("/vieneu/profiles/{voice_id}")
    async def delete_profile(voice_id: str):
        async with runtime.lock:
            try:
                runtime.store.delete(voice_id)
                return {"deleted": True, "voice_id": voice_id}
            except VieNeuError as exc:
                return fail(_status(exc), exc.code)

    async def _speech(body: VieNeuSpeech, request: Request):
        try:
            jobid = request.headers.get("X-TTS-Request-Id") or uuid.uuid4().hex
            token = request.headers.get("X-TTS-Cancel-Token")
            if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", jobid):
                return fail(422, "INVALID_CANCEL_IDENTITY")
            if token is not None and not re.fullmatch(r"[A-Za-z0-9_-]{24,256}", token):
                return fail(422, "INVALID_CANCEL_IDENTITY")
            if app.state.vieneu_active is not None or runtime.lock.locked():
                result = fail(429, "WORKER_BUSY")
                result.headers["Retry-After"] = "1"
                return result
            task = asyncio.create_task(runtime.synthesize(text=body.input, voice_id=body.voice,
                           speed=body.speed, steps=body.steps, cfg=body.cfg, sway=body.sway, seed=body.seed))
            owner = {"request_id": jobid, "cancel_token": token or uuid.uuid4().hex,
                     "task": task}
            app.state.vieneu_active = owner
            while not task.done():
                if await request.is_disconnected():
                    task.cancel()
                    with contextlib.suppress(BaseException):
                        await task
                    return fail(499, "CLIENT_DISCONNECTED")
                await asyncio.wait({task}, timeout=0.1)
            output, info = await task
            receipt = info["receipt"]
            payload = json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            tokenized = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
            spoken = build_job(body.input).spoken
            syllables = sum(len(part.split()) for part in spoken)
            spoken_seconds = max(receipt["audio_seconds"] - receipt["pause_frames"] / 24000, 0.01)
            return Response(output, media_type="audio/wav", headers={
                "Cache-Control": "no-store", "X-TTS-Engine": PROVIDER,
                "X-TTS-Backend": "CPUExecutionProvider", "X-TTS-Model-Revision": MODEL_REVISION,
                "X-TTS-Audio-S": str(round(receipt["audio_seconds"], 4)),
                "X-TTS-RTF": str(round(receipt["end_to_end_rtf"], 4)),
                "X-TTS-Approx-Syllables-Per-Minute": str(round(syllables * 60 / spoken_seconds, 2)),
                "X-VieNeu-Contract": CONTRACT, "X-VieNeu-Receipt": tokenized,
                "X-VieNeu-Receipt-SHA256": __import__("hashlib").sha256(payload).hexdigest()})
        except asyncio.CancelledError:
            return fail(499, "CANCELLED")
        except (VieNeuError, PauseError) as exc:
            return fail(_status(exc) if isinstance(exc, VieNeuError) else 422,
                        exc.code)
        except (ValueError, AudioError) as exc:
            return fail(422, "INVALID_TEXT" if isinstance(exc, ValueError) else "AUDIO_INTEGRITY_FAILURE")
        finally:
            if app.state.vieneu_active is not None and app.state.vieneu_active.get("request_id") == locals().get("jobid"):
                app.state.vieneu_active = None

    @router.post("/vieneu/v1/audio/speech")
    async def speech(body: VieNeuSpeech, request: Request):
        return await _speech(body, request)

    @router.post("/vieneu/profiles/{voice_id}/preview")
    async def preview(voice_id: str, request: Request):
        return await _speech(VieNeuSpeech(voice=voice_id, input="Xin chào. Đây là bản nghe thử giọng nói tiếng Việt."), request)

    app.include_router(router)
    return runtime
