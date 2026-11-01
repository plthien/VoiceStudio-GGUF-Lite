"""Localhost-only, blocking OpenAI-compatible minimal GGUF Lite API."""
from __future__ import annotations
import asyncio
import contextlib
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, field_validator

from .config import Settings, REPO_ID, BASE_NAME, TOKENIZER_NAME
from .licence import Licence
from .native_runner import Runner, NativeBusy, NativeFailure, NativeTimeout
from .profiles import Store, ProfileError
from .runtime import preflight, RuntimeErrorLite

logger = logging.getLogger("gguf_lite")

def failure(status: int, code: str, message: str, retry: bool = False):
    headers = {"Retry-After": "3"} if retry else None
    return JSONResponse(status_code=status, content={"error": {
        "message": message, "type": "lite_error", "code": code}}, headers=headers)

class Speech(BaseModel):
    model: str
    voice: str = Field(min_length=1, max_length=128)
    input: str = Field(min_length=1, max_length=4096)
    response_format: str = "wav"
    speed: float = 1.0
    language: str | None = None
    seed: int | None = Field(default=None, ge=0, le=2147483647)

    @field_validator("model")
    @classmethod
    def model_name(cls, value):
        if value != "omnivoice-gguf":
            raise ValueError("Only omnivoice-gguf is supported")
        return value

    @field_validator("response_format")
    @classmethod
    def wav_only(cls, value):
        if value != "wav":
            raise ValueError("Only WAV is supported")
        return value

    @field_validator("speed")
    @classmethod
    def fixed_speed(cls, value):
        if value != 1.0:
            raise ValueError("Native GGUF does not support speed control")
        return value

    @field_validator("input")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Input must contain text")
        return value

class ProfilePatch(BaseModel):
    name: str | None = None
    ref_text: str | None = None
    language: str | None = None
    consent: str | None = None

class AcceptTerms(BaseModel):
    repo_id: str
    fingerprint: str
    accepted: bool

def create_app(settings: Settings | None = None, store: Store | None = None,
               licence: Licence | None = None, runner: Runner | None = None) -> FastAPI:
    settings = settings or Settings.load()
    @asynccontextmanager
    async def lifespan(application):
        yield
        await application.state.runner.stop()

    app = FastAPI(title="VoiceStudio GGUF Lite", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store or Store(settings)
    app.state.licence = licence or Licence(settings)
    app.state.runner = runner or Runner(settings)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = request.client.host if request.client else "127.0.0.1"
        if host not in ("127.0.0.1", "::1", "testclient"):
            return failure(403, "LOCAL_ONLY", "Loopback clients only")
        if request.headers.get("content-length"):
            try:
                if int(request.headers["content-length"]) > settings.max_ref_bytes + 65536:
                    return failure(413, "BODY_TOO_LARGE", "Upload too large")
            except ValueError:
                return failure(400, "BAD_LENGTH", "Invalid content length")
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, _exc):
        return failure(422, "INVALID_REQUEST", "Request failed validation")

    @app.get("/health")
    def health():
        return {"status": "alive", "ready": False}

    @app.get("/lite/readiness")
    async def readiness():
        state = await asyncio.to_thread(preflight, settings)
        state["licence"] = app.state.licence.status()
        state["voice_store"] = {"healthy": settings.data_dir.is_dir()}
        state["busy"] = app.state.runner.lock.locked()
        state["ready"] = state["ready"] and app.state.licence.is_ready() and state["voice_store"]["healthy"]
        return state

    @app.get("/engines/omnivoice-gguf/quants")
    async def quants():
        state = await asyncio.to_thread(preflight, settings)
        return {"selected": BASE_NAME, "in_use": BASE_NAME if state["ready"] else None,
                "base": BASE_NAME, "tokenizer": TOKENIZER_NAME, "ready": state["ready"]}

    @app.get("/v1/audio/voices")
    def voices():
        return [{"id": v["id"], "voice_id": v["id"], "name": v["name"],
                 "language": v["language"]} for v in app.state.store.list()]

    @app.get("/profiles")
    def profiles():
        return app.state.store.list()

    @app.get("/profiles/{profile_id}")
    def get_profile(profile_id: str):
        row = app.state.store.get(profile_id)
        return row if row else failure(404, "PROFILE_NOT_FOUND", "Unknown voice ID")

    async def upload(name: str, ref_text: str, language: str, ref_audio: UploadFile,
                     profile_id: str | None = None):
        if not ref_audio.filename:
            return failure(422, "AUDIO_REQUIRED", "WAV or MP3 is required")
        suffix = Path(ref_audio.filename).suffix.lower()
        payload = await ref_audio.read(settings.max_ref_bytes + 1)
        if len(payload) > settings.max_ref_bytes:
            return failure(413, "AUDIO_TOO_LARGE", "Reference is too large")
        if profile_id is not None and profile_id == app.state.runner.active_id:
            return failure(409, "PROFILE_ACTIVE", "Voice in active inference")
        try:
            return await asyncio.to_thread(app.state.store.import_audio,
                name=name, ref_text=ref_text, language=language, source=payload,
                suffix=suffix, profile_id=profile_id)
        except KeyError:
            return failure(404, "PROFILE_NOT_FOUND", "Unknown voice ID")
        except (ValueError, OSError) as exc:
            return failure(422, "INVALID_AUDIO", str(exc) if isinstance(exc, ProfileError) else "Invalid reference audio")

    @app.post("/profiles")
    async def create_profile(name: str = Form(...), ref_text: str = Form(...),
                             language: str = Form("en"), ref_audio: UploadFile = File(...)):
        return await upload(name, ref_text, language, ref_audio)

    @app.put("/profiles/{profile_id}/audio")
    async def replace_audio(profile_id: str, ref_audio: UploadFile = File(...)):
        row = app.state.store.get(profile_id)
        if not row:
            return failure(404, "PROFILE_NOT_FOUND", "Unknown voice ID")
        return await upload(row["name"], row["ref_text"], row["language"], ref_audio, profile_id)

    @app.put("/profiles/{profile_id}")
    def update_profile(profile_id: str, patch: ProfilePatch):
        if profile_id == app.state.runner.active_id:
            return failure(409, "PROFILE_ACTIVE", "Voice in active inference")
        try:
            return app.state.store.update(profile_id, patch.model_dump(exclude_unset=True))
        except KeyError:
            return failure(404, "PROFILE_NOT_FOUND", "Unknown voice ID")
        except ProfileError as exc:
            return failure(422, "INVALID_PROFILE", str(exc))

    @app.delete("/profiles/{profile_id}")
    def delete_profile(profile_id: str):
        try:
            app.state.store.delete(profile_id, active_id=app.state.runner.active_id)
        except KeyError:
            return failure(404, "PROFILE_NOT_FOUND", "Unknown voice ID")
        except ProfileError:
            return failure(409, "PROFILE_ACTIVE", "Voice in active inference")
        return {"deleted": True, "id": profile_id}

    @app.get("/models/licenses/details/{repo_id:path}")
    def licence_details(repo_id: str):
        if repo_id != REPO_ID:
            return failure(404, "MODEL_NOT_FOUND", "Unknown model licence")
        return app.state.licence.details()

    @app.get("/models/licenses/acceptance/{repo_id:path}")
    def licence_status(repo_id: str):
        if repo_id != REPO_ID:
            return failure(404, "MODEL_NOT_FOUND", "Unknown model licence")
        return app.state.licence.status()

    @app.post("/models/licenses/accept")
    def accept(payload: AcceptTerms):
        if payload.repo_id != REPO_ID:
            return failure(404, "MODEL_NOT_FOUND", "Unknown model licence")
        try:
            return app.state.licence.change(
                expected=payload.fingerprint,
                action="accepted" if payload.accepted else "withdrawn")
        except ValueError:
            return failure(409, "TERMS_CHANGED", "Licence terms fingerprint mismatch")

    @app.post("/v1/audio/speech")
    async def speech(payload: Speech):
        state = app.state
        if state.runner.lock.locked():
            return failure(429, "NATIVE_BUSY", "One native inference already active", retry=True)
        row = state.store.get(payload.voice)
        if not row:
            return failure(404, "PROFILE_NOT_FOUND", "Unknown voice ID")
        if not state.licence.is_ready():
            return failure(403, "LICENCE_NOT_READY", "Model terms require verified provenance and explicit acceptance")
        try:
            reference = state.store.reference(row)
        except ProfileError:
            return failure(409, "REFERENCE_INVALID", "Stored reference is invalid")
        try:
            data, info = await state.runner.generate(
                profile=row, reference=reference, text=payload.input,
                language=payload.language, seed=payload.seed,
                request_id=str(uuid.uuid4()))
            return Response(data, media_type="audio/wav", headers={
                "X-Lite-Request-Id": info["request_id"], "Cache-Control": "no-store"})
        except NativeBusy:
            return failure(429, "NATIVE_BUSY", "One native inference already active", retry=True)
        except NativeTimeout:
            return failure(504, "NATIVE_TIMEOUT", "Native inference deadline exceeded")
        except RuntimeErrorLite:
            return failure(503, "RUNTIME_NOT_READY", "Pinned Vulkan Q8_0 runtime not ready")
        except NativeFailure:
            return failure(500, "NATIVE_FAILURE", "Native inference failed")
        except (OSError, ValueError):
            logger.exception("native failure request=<redacted>")
            return failure(500, "INTERNAL_ERROR", "Native operation failed")
    return app

# Lazy: no native model load, no network and no heavy ML modules.
app = create_app()
