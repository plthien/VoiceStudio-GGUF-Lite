"""Localhost Chatterbox-Nano API; no GGUF application initialization."""
from __future__ import annotations

import asyncio
import contextlib
import io
import math
import sqlite3
import os
import shutil
import tempfile
import time
import wave
import hashlib
import base64
import hmac
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, RedirectResponse
from pydantic import BaseModel, Field, field_validator

from .audio import AudioError, convert_reference, wav_info
from .chatterbox_config import (ChatterboxSettings, MODEL_ID, MODEL_REPO,
    MODEL_HASHES, MODEL_REVISION, SOURCE_REVISION, diagnostic, ffmpeg_path, IDLE_CHOICES)
from .chatterbox_service import TurboService, TurboError
from .chatterbox_segmentation import (HARD_CHARS, TARGET_CHARS, POLICY_VERSION,
    segment_text, SegmentationError, SUPPORTED_TAGS)
from .chatterbox_migration import legacy_sources, preview as migration_preview, apply as migration_apply
from .chatterbox_assembly import atomic, reference_binding
from .chatterbox_contract import (CONTRACT_VERSION, SPEED_POLICY_VERSION,
    build_receipt, frame_seconds, speed_ascii, weights_manifest_sha256)
from .pause import parse_pause_script, speech_text, PauseError, DEFAULT_PAUSE_MS, MAX_EVENT_MS, MAX_MARKERS, MAX_TOTAL_MS, PAUSE_POLICY_VERSION
from .profiles import Store, ProfileError

UI_DIR = Path(__file__).with_name("chatterbox_ui")
HEADERS = {"Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
           "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
           "Cache-Control": "no-store"}


def failure(status, code, message=None):
    return JSONResponse({"error": {"type": "chatterbox_error", "code": code,
                                    "message": message or code}}, status_code=status,
                        headers={"Cache-Control": "no-store"})


class SizeGuard:
    def __init__(self, app, limit):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        try:
            length = next((v for k, v in scope.get("headers", []) if k.lower() == b"content-length"), None)
            if length is not None and (int(length) < 0 or int(length) > self.limit):
                return await failure(413, "BODY_TOO_LARGE")(scope, receive, send)
        except ValueError:
            return await failure(400, "BAD_LENGTH")(scope, receive, send)
        total = 0
        started = False
        async def bounded():
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.limit:
                    raise ValueError("BODY_TOO_LARGE")
            return message
        async def out(msg):
            nonlocal started
            started |= msg["type"] == "http.response.start"
            await send(msg)
        try:
            return await self.app(scope, bounded, out)
        except ValueError as exc:
            if not started and str(exc) == "BODY_TOO_LARGE":
                return await failure(413, "BODY_TOO_LARGE")(scope, receive, send)
            raise


class AdmissionGate:
    """Event-loop-local atomic no-wait ownership gate.

    ``try_acquire`` contains no await point, so a returned busy result proves
    that the rejected request was never queued for worker admission.
    """
    def __init__(self):
        self._owner = None

    def locked(self):
        return self._owner is not None

    def try_acquire(self):
        if self._owner is not None:
            return None
        owner = object()
        self._owner = owner
        return owner

    def release(self, owner):
        if self._owner is not owner:
            raise RuntimeError("ADMISSION_OWNER_MISMATCH")
        self._owner = None


class Speech(BaseModel):
    model: str
    voice: str = Field(min_length=1, max_length=128)
    input: str = Field(min_length=1, max_length=4096)
    response_format: str = "wav"
    speed: float = 1.0
    seed: int | None = Field(42, ge=0, le=2147483647)
    language: str = "en"

    @field_validator("model")
    @classmethod
    def valid_model(cls, value):
        if value != MODEL_ID:
            raise ValueError("Only chatterbox-nano")
        return value

    @field_validator("response_format")
    @classmethod
    def valid_format(cls, value):
        if value != "wav":
            raise ValueError("WAV only")
        return value

    @field_validator("language")
    @classmethod
    def valid_language(cls, value):
        if value != "en":
            raise ValueError("English only")
        return value

    @field_validator("speed")
    @classmethod
    def valid_speed(cls, value):
        if not math.isfinite(value) or not 0.75 <= value <= 1.25:
            raise ValueError("Speed out of bounds")
        return value

    @field_validator("input")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Empty input")
        return value


class IdleSetting(BaseModel):
    idle_timeout_seconds: int | None = 600

    @field_validator("idle_timeout_seconds")
    @classmethod
    def valid_timeout(cls, value):
        if value not in IDLE_CHOICES:
            raise ValueError("Unsupported idle interval")
        return value


class ProfilePatch(BaseModel):
    name: str | None = None
    ref_text: str | None = None
    language: str | None = None
    consent: str | None = None
    confirm_transcript_change: bool = False


class CancelRequest(BaseModel):
    request_id: str = Field(min_length=8, max_length=128,
                            pattern=r"^[A-Za-z0-9._~-]+$")
    cancel_token: str = Field(min_length=22, max_length=128,
                              pattern=r"^[A-Za-z0-9_-]+$")

    @field_validator("cancel_token")
    @classmethod
    def valid_secret(cls, value):
        try:
            raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        except (ValueError, TypeError) as exc:
            raise ValueError("Invalid cancel token") from exc
        if len(raw) < 16:
            raise ValueError("Cancel token needs 128 bits")
        return value


class MigrationSelection(BaseModel):
    voice_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")


def public_profile(row):
    if not row:
        return row
    return {key: row[key] for key in ("id", "name", "language", "ref_text", "ref_sha256",
            "source_sha256", "version", "consent", "created", "updated") if key in row}


def create_app(settings: ChatterboxSettings | None = None, store=None, service=None):
    settings = settings or ChatterboxSettings.load()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    @asynccontextmanager
    async def lifespan(app):
        yield
        await app.state.service.stop()
    app = FastAPI(title="VoiceStudio Nano Lite", version="1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store or Store(settings)
    app.state.service = service or TurboService(settings)
    app.state.admission = AdmissionGate()
    app.state.active_generation = None
    app.state.cache_trusted = True
    app.state.settings_consistent = True
    app.state.settings_path = settings.data_dir / "chatterbox-settings.json"
    try:
        import json
        obj = json.loads(app.state.settings_path.read_text(encoding="utf-8"))
        if obj.get("schema") != "chatterbox-settings-v1" or obj.get("idle_timeout_seconds") not in IDLE_CHOICES:
            raise ValueError()
        app.state.service.set_idle(obj["idle_timeout_seconds"])
    except FileNotFoundError:
        pass

    @app.middleware("http")
    async def local_only(request: Request, next_handler):
        peer = request.client.host if request.client else ""
        if peer not in ("127.0.0.1", "::1", "testclient"):
            return failure(403, "LOCAL_ONLY")
        host = request.headers.get("host", "").lower()
        authority = host.split(":")[0] if not host.startswith("[") else host.split("]")[0] + "]"
        if authority not in ("127.0.0.1", "localhost", "[::1]") and not (peer == "testclient" and authority == "testserver"):
            return failure(403, "UNTRUSTED_HOST")
        if host.endswith(":"):
            return failure(400, "INVALID_HOST")
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("origin")
            site = request.headers.get("sec-fetch-site", "").lower()
            if site not in ("", "none", "same-origin"):
                return failure(403, "CROSS_SITE_BLOCKED")
            if origin:
                try:
                    o = urlsplit(origin)
                    if o.scheme != "http" or o.netloc.lower() != host or o.path or o.query or o.fragment:
                        return failure(403, "CROSS_ORIGIN_BLOCKED")
                except ValueError:
                    return failure(403, "CROSS_ORIGIN_BLOCKED")
        return await next_handler(request)

    @app.exception_handler(RequestValidationError)
    async def request_error(_request, _exc):
        return failure(422, "INVALID_REQUEST")

    @app.get("/")
    def homepage():
        return RedirectResponse("/ui", headers=HEADERS)

    @app.get("/ui")
    def ui():
        return Response((UI_DIR / "index.html").read_bytes(), media_type="text/html", headers=HEADERS)

    @app.get("/ui/assets/{name}")
    def asset(name: str):
        kinds = {"app.js": "text/javascript", "app.css": "text/css"}
        if name not in kinds:
            return Response(status_code=404, headers=HEADERS)
        return Response((UI_DIR / name).read_bytes(), media_type=kinds[name], headers=HEADERS)

    @app.get("/health")
    def health():
        return {"status": "alive", "ready": False}

    @app.get("/lite/readiness")
    @app.get("/chatterbox/readiness", include_in_schema=False)
    def readiness():
        status=app.state.service.status()
        result=diagnostic(settings)
        model_verified=bool(status.get("model_hash_verified"))
        cuda_verified=bool(status.get("cuda_verified"))
        eligible=bool(result["installation_preflight_ready"]
                      and app.state.cache_trusted and app.state.settings_consistent)
        result["contract_version"] = CONTRACT_VERSION
        result["installation_prerequisites_ready"] = result["installation_preflight_ready"]
        result["model_hash_verified"] = model_verified
        result["cuda_verified"] = cuda_verified
        result["worker_resident"] = bool(status.get("resident"))
        result["worker_busy"] = bool(status.get("busy") or app.state.admission.locked())
        result["cold_load_eligible"] = eligible
        result["runtime_ready"]=eligible
        result["generation_ready"]=eligible
        result["tts_ready"]=eligible
        result["live_generation_verified"] = bool(status.get("live_generation_verified"))
        result["readiness_semantics"] = "eligible_to_attempt_cold_load_not_cuda_attestation"
        result["cloning_ready"]="FFMPEG_UNAVAILABLE" not in result["issues"]
        result["ready"]=eligible
        result["service_recovery_required"]=not (
            app.state.cache_trusted and app.state.settings_consistent)
        return {**result,"worker":status}

    @app.get("/lite/status")
    @app.get("/lite/worker/status")
    @app.get("/chatterbox/runtime/status", include_in_schema=False)
    def runtime_status():
        return {**app.state.service.status(), "model": MODEL_ID, "backend": "CUDA0",
                "voice_count": len(app.state.store.list()),
                "ffmpeg_installed": bool(ffmpeg_path()),
                "admission_busy": app.state.admission.locked(),
                "contract_version": CONTRACT_VERSION}

    @app.get("/lite/model-capability")
    @app.get("/chatterbox/capability", include_in_schema=False)
    def capability():
        return {"contract_version": CONTRACT_VERSION, "model": MODEL_ID,
                "repo_id": MODEL_REPO, "language": "en", "backend": "CUDA0",
                "loader_mode": "nano=True", "model_revision": MODEL_REVISION,
                "source_revision": SOURCE_REVISION, "weights_sha256": MODEL_HASHES,
                "weights_manifest_sha256": weights_manifest_sha256(),
                "sample_rate": 24000, "channels": 1, "pcm_bits": 16,
                "speed_mode": "postprocess_atempo", "speed_min": 0.75, "speed_max": 1.25,
                "pause_policy": PAUSE_POLICY_VERSION, "default_pause_ms": DEFAULT_PAUSE_MS,
                "max_pause_ms": MAX_EVENT_MS, "max_pause_count": MAX_MARKERS,
                "max_pause_total_ms": MAX_TOTAL_MS, "max_chars": 4096,
                "api_max_input_characters": 4096,
                "definite_429_nonadmission": True,
                "native_segment_target_chars": TARGET_CHARS,
                "native_segment_hard_chars": HARD_CHARS,
                "segmentation_policy": POLICY_VERSION,
                "supported_paralinguistic_tokens": sorted(SUPPORTED_TAGS)}

    @app.get("/lite/speed-capability")
    def speed_capability():
        return {"supported": True, "mode": "postprocess_atempo", "min": 0.75,
                "max": 1.25, "policy_version": SPEED_POLICY_VERSION,
                "applies_to": "speech_only", "authored_pauses_affected": False}

    @app.get("/lite/pause-capability")
    def pause_capability():
        return {"supported": True, "policy": PAUSE_POLICY_VERSION, "sample_rate": 24000,
                "policy_version": PAUSE_POLICY_VERSION, "sample_rate_hz": 24000,
                "default_ms": DEFAULT_PAUSE_MS, "max_event_ms": MAX_EVENT_MS,
                "max_markers": MAX_MARKERS, "max_total_ms": MAX_TOTAL_MS,
                "pcm_sample_value": 0, "channels": 1, "sample_width_bytes": 2,
                "grammar": ["[pause]", "[pause 800ms]", "[pause 1.5s]"]}

    @app.get("/lite/worker/settings")
    @app.get("/chatterbox/runtime/settings", include_in_schema=False)
    def get_settings():
        return {"schema": "chatterbox-settings-v1", "idle_timeout_seconds": app.state.service.idle_timeout}

    @app.put("/lite/worker/settings")
    @app.put("/chatterbox/runtime/settings", include_in_schema=False)
    async def put_settings(change: IdleSetting):
        import json
        if not app.state.settings_consistent:
            return failure(503,"SETTINGS_RECOVERY_REQUIRED")
        owner = app.state.admission.try_acquire()
        if owner is None or app.state.service.lock.locked():
            if owner is not None:
                app.state.admission.release(owner)
            return failure(409,"WORKER_BUSY")
        try:
            previous=app.state.service.idle_timeout
            try:
                old_bytes=(await asyncio.to_thread(app.state.settings_path.read_bytes)
                           if app.state.settings_path.exists() else None)
            except OSError:
                return failure(500,"SETTINGS_SAVE_FAILED")
            obj={"schema":"chatterbox-settings-v1",
                 "idle_timeout_seconds":change.idle_timeout_seconds}
            try:
                await asyncio.to_thread(atomic,app.state.settings_path,json.dumps(obj).encode())
            except (OSError,ValueError):
                return failure(500,"SETTINGS_SAVE_FAILED")
            try:
                app.state.service.set_idle(change.idle_timeout_seconds)
            except BaseException:
                try:
                    if old_bytes is None:
                        await asyncio.to_thread(app.state.settings_path.unlink,missing_ok=True)
                    else:
                        await asyncio.to_thread(atomic,app.state.settings_path,old_bytes)
                    app.state.service.set_idle(previous)
                except BaseException:
                    app.state.settings_consistent=False
                    with contextlib.suppress(BaseException):
                        await app.state.service.stop()
                    return failure(503,"SETTINGS_RECOVERY_REQUIRED")
                return failure(500,"SETTINGS_SAVE_FAILED")
            return obj
        finally:
            app.state.admission.release(owner)

    @app.post("/lite/worker/unload")
    @app.post("/chatterbox/runtime/unload", include_in_schema=False)
    async def unload():
        owner = app.state.admission.try_acquire()
        if owner is None:
            return failure(409, "WORKER_BUSY")
        try:
            return await app.state.service.unload_now()
        except TurboError:
            return failure(409, "WORKER_BUSY")
        finally:
            app.state.admission.release(owner)

    @app.post("/lite/worker/cancel")
    @app.post("/chatterbox/runtime/cancel", include_in_schema=False)
    async def cancel(change: CancelRequest):
        active = app.state.active_generation
        if active is None or active["task"].done():
            return failure(404, "NO_ACTIVE_REQUEST")
        if active["request_id"] is None:
            return failure(409, "CANCELLATION_NOT_ENABLED")
        if (not hmac.compare_digest(change.request_id, active["request_id"])
                or not hmac.compare_digest(change.cancel_token, active["cancel_token"])):
            return failure(403, "CANCEL_NOT_AUTHORIZED")
        active["task"].cancel()
        # The request task remains the only teardown owner; this acknowledgment
        # does not claim that CUDA cleanup has completed yet.
        return {"cancelled": True, "state": "cancelling", "request_id": change.request_id}

    @app.get("/lite/rights")
    def rights():
        return {"application_license": "AGPL-3.0", "model_license": "MIT",
                "model_repo": MODEL_REPO, "model_revision": MODEL_REVISION,
                "source_revision": SOURCE_REVISION, "voice_consent_required": True}

    @app.get("/lite/migration/preview")
    def migration_dry_run():
        try:
            database, voices_dir = legacy_sources()
            return migration_preview(database, voices_dir)
        except (OSError, ValueError, sqlite3.Error):
            return {"configured": False, "eligible": [], "invalid": [],
                    "issue": "LEGACY_SOURCE_UNAVAILABLE"}

    @app.post("/lite/migration/apply")
    async def migration_selected(selection: MigrationSelection):
        def mutation():
            database, voices_dir = legacy_sources()
            return migration_apply(database, voices_dir, settings, selection.voice_id)
        try:
            return await owned_mutation(mutation, invalidate_voice=selection.voice_id)
        except (OSError, ValueError, sqlite3.Error):
            return failure(409, "LEGACY_IMPORT_FAILED")

    @app.get("/profiles")
    def profiles():
        return [public_profile(row) for row in app.state.store.list()]

    @app.get("/v1/audio/voices")
    def voices():
        return [{"id": r["id"], "voice_id": r["id"], "name": r["name"], "language": r["language"]}
                for r in app.state.store.list()]

    @app.get("/profiles/{voice_id}")
    def get_voice(voice_id: str):
        row = app.state.store.get(voice_id)
        return public_profile(row) if row else failure(404, "PROFILE_NOT_FOUND")

    @app.post("/profiles/{voice_id}/integrity/verify")
    async def verify_owned_reference(voice_id: str):
        """Read-only actual-byte proof. Admission excludes trusted profile mutations.
        No Chatterbox/Torch import or inference; no filesystem path disclosure.
        """
        if not app.state.cache_trusted or not app.state.settings_consistent:
            return failure(503, "SERVICE_RECOVERY_REQUIRED")
        owner = app.state.admission.try_acquire()
        if owner is None or app.state.service.lock.locked():
            if owner is not None:
                app.state.admission.release(owner)
            return failure(503, "REFERENCE_VERIFICATION_BUSY")
        try:
            try:
                row=await asyncio.to_thread(app.state.store.get,voice_id)
            except (OSError,sqlite3.Error):
                return failure(503, "REFERENCE_VERIFICATION_UNAVAILABLE")
            if row is None:
                return failure(404, "PROFILE_NOT_FOUND")
            if row.get("consent") != "confirmed" or row.get("language") != "en":
                return failure(409, "REFERENCE_CHANGED")
            try:
                # Store.reference resolves the path inside the voice root and
                # hashes the actual normalized WAV; no metadata-only attestation.
                await asyncio.to_thread(app.state.store.reference, row)
                latest=await asyncio.to_thread(app.state.store.get,voice_id)
                binding=reference_binding(row,voice_id)
                if latest is None or reference_binding(latest,voice_id)!=binding:
                    return failure(409, "REFERENCE_CHANGED")
            except (OSError, sqlite3.Error):
                return failure(503, "REFERENCE_VERIFICATION_UNAVAILABLE")
            except (ProfileError, KeyError, ValueError, RuntimeError):
                return failure(409, "REFERENCE_CHANGED")
            return {"verified":True,"binding":binding, "voice_id":row["id"],
                    "voice_version":row["version"],
                    "reference_sha256":row["ref_sha256"]}
        finally:
            app.state.admission.release(owner)

    @app.get("/profiles/{voice_id}/reference")
    def preview(voice_id: str):
        row = app.state.store.get(voice_id)
        if not row:
            return failure(404, "PROFILE_NOT_FOUND")
        try:
            data = app.state.store.reference(row).read_bytes()
            wav_info(data, max_bytes=settings.max_ref_bytes + 65536, max_seconds=60)
            return Response(data, media_type="audio/wav", headers={"Cache-Control": "no-store"})
        except (ProfileError, OSError, AudioError):
            return failure(409, "REFERENCE_INVALID")

    async def owned_mutation(fun, invalidate_voice=None, *, invalidate=True):
        """Keep admission owned until cache invalidation and acknowledgment."""
        admission_owner = app.state.admission.try_acquire()
        if admission_owner is None or app.state.service.lock.locked():
            if admission_owner is not None:
                app.state.admission.release(admission_owner)
            return failure(409,"PROFILE_ACTIVE")
        async def owner():
            try:
                try:
                    changed=await asyncio.to_thread(fun)
                except BaseException as exc:
                    # A Store operation can commit SQLite and then fail cleaning files.
                    # Never let a resident worker retain conditionals for that outcome.
                    try:
                        await app.state.service.stop()
                    except BaseException:
                        app.state.cache_trusted=False
                        raise TurboError("MUTATION_OUTCOME_UNKNOWN_WORKER_UNSAFE") from exc
                    if isinstance(exc,ProfileError) and "Profile removed but" in str(exc):
                        raise TurboError("MUTATION_COMMITTED_CLEANUP_REQUIRED") from exc
                    raise
                voice=invalidate_voice or (changed.get("id") if isinstance(changed,dict) else None)
                if not invalidate:
                    return changed
                try:
                    await app.state.service.invalidate(voice)
                except BaseException:
                    try:
                        await app.state.service.stop()
                    except BaseException:
                        app.state.cache_trusted=False
                        raise TurboError("MUTATION_COMMITTED_WORKER_UNSAFE")
                    raise TurboError("MUTATION_COMMITTED_CACHE_RESET")
                return changed
            finally:
                app.state.admission.release(admission_owner)
        try:
            task=asyncio.create_task(owner())
        except BaseException:
            app.state.admission.release(admission_owner)
            raise
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        return await asyncio.shield(task)

    def check_consent(value):
        return isinstance(value, str) and value == "confirmed"

    def request_cancel_identity(request: Request):
        request_id = request.headers.get("x-tts-request-id")
        cancel_token = request.headers.get("x-tts-cancel-token")
        if request_id is None and cancel_token is None:
            return None, None
        if request_id is None or cancel_token is None:
            raise ValueError("CANCEL_IDENTITY_INCOMPLETE")
        if not re.fullmatch(r"[A-Za-z0-9._~-]{8,128}", request_id):
            raise ValueError("INVALID_REQUEST_ID")
        if not re.fullmatch(r"[A-Za-z0-9_-]{22,128}", cancel_token):
            raise ValueError("INVALID_CANCEL_TOKEN")
        try:
            secret = base64.urlsafe_b64decode(cancel_token + "=" * (-len(cancel_token) % 4))
        except (ValueError, TypeError) as exc:
            raise ValueError("INVALID_CANCEL_TOKEN") from exc
        if len(secret) < 16 or hmac.compare_digest(request_id, cancel_token):
            raise ValueError("INVALID_CANCEL_TOKEN")
        return request_id, cancel_token

    async def import_voice(name, ref_text, language, ref_audio, consent, voice_id=None):
        if not check_consent(consent):
            return failure(422, "VOICE_CONSENT_REQUIRED")
        if language != "en":
            return failure(422, "ENGLISH_ONLY")
        if not ref_audio.filename or Path(ref_audio.filename).suffix.lower() not in (".wav", ".mp3"):
            return failure(422, "INVALID_AUDIO")
        suffix = Path(ref_audio.filename).suffix.lower()
        content = await ref_audio.read(settings.max_ref_bytes + 1)
        if len(content) > settings.max_ref_bytes:
            return failure(413, "AUDIO_TOO_LARGE")
        if not ref_text or not ref_text.strip():
            return failure(422, "TRANSCRIPT_REQUIRED")
        def mutation():
            with tempfile.TemporaryDirectory(dir=settings.data_dir) as folder:
                original = Path(folder) / ("input" + suffix)
                normalized = Path(folder) / "normalized.wav"
                original.write_bytes(content)
                try:
                    info = convert_reference(original, normalized, ffmpeg=ffmpeg_path() or "",
                                             max_seconds=settings.max_ref_seconds)
                except (AudioError, OSError) as exc:
                    raise ProfileError("INVALID_AUDIO") from exc
                if info["seconds"] <= 5:
                    raise ProfileError("REFERENCE_TOO_SHORT")
            if voice_id:
                previous = app.state.store.get(voice_id)
                if not previous:
                    raise KeyError(voice_id)
                name_local = previous["name"]
            else:
                name_local = name
            return app.state.store.import_audio(
                name=name_local, ref_text=ref_text, language="en", source=content,
                suffix=suffix, consent="confirmed", profile_id=voice_id)
        try:
            result = await owned_mutation(mutation,invalidate_voice=voice_id)
            return public_profile(result) if isinstance(result, dict) else result
        except TurboError as exc:
            return failure(503,exc.code)
        except KeyError:
            return failure(404, "PROFILE_NOT_FOUND")
        except ProfileError as exc:
            return failure(422, str(exc) if str(exc) == "REFERENCE_TOO_SHORT" else "INVALID_AUDIO")
        except (OSError, ValueError):
            return failure(422, "INVALID_AUDIO")

    @app.post("/profiles")
    async def create_voice(name: str = Form(...), ref_text: str = Form(...),
                           ref_audio: UploadFile = File(...), language: str = Form("en"),
                           consent: str = Form(...)):
        return await import_voice(name, ref_text, language, ref_audio, consent)

    @app.put("/profiles/{voice_id}/audio")
    async def replace_voice(voice_id: str, ref_audio: UploadFile = File(...),
                            ref_text: str | None = Form(None), consent: str = Form(...),
                            same_words: bool = Form(False)):
        previous = app.state.store.get(voice_id)
        if not previous:
            return failure(404, "PROFILE_NOT_FOUND")
        if same_words and ref_text and ref_text.strip() != previous["ref_text"].strip():
            return failure(422, "TRANSCRIPT_CONFIRMATION_CONFLICT")
        transcript = previous["ref_text"] if same_words else ref_text
        if not transcript:
            return failure(422, "TRANSCRIPT_REQUIRED")
        return await import_voice("", transcript, "en", ref_audio, consent, voice_id)

    @app.put("/profiles/{voice_id}")
    async def edit_voice(voice_id: str, change: ProfilePatch):
        data = change.model_dump(exclude_unset=True)
        confirmed = bool(data.pop("confirm_transcript_change", False))
        if data.get("language", "en") != "en" or data.get("consent", "confirmed") != "confirmed":
            return failure(422, "INVALID_PROFILE")
        current = app.state.store.get(voice_id)
        if not current:
            return failure(404, "PROFILE_NOT_FOUND")
        transcript_changed = "ref_text" in data and data["ref_text"] != current["ref_text"]
        if transcript_changed and not confirmed:
            return failure(422, "TRANSCRIPT_CHANGE_CONFIRMATION_REQUIRED")
        try:
            identity_change = bool(set(data) - {"name"})
            changed = await owned_mutation(
                lambda: app.state.store.update(voice_id,data), invalidate_voice=voice_id,
                invalidate=identity_change)
            return public_profile(changed) if isinstance(changed, dict) else changed
        except TurboError as exc:
            return failure(503,exc.code)
        except KeyError:
            return failure(404, "PROFILE_NOT_FOUND")
        except ProfileError:
            return failure(422, "INVALID_PROFILE")

    @app.delete("/profiles/{voice_id}")
    async def delete_voice(voice_id: str, purge: bool = False):
        try:
            deleted=await owned_mutation(lambda: app.state.store.delete(
                voice_id,active_id=app.state.service.active_id,purge=purge),
                invalidate_voice=voice_id)
            if isinstance(deleted,Response):
                return deleted
            return {"deleted":True,"owned_reference_files_purged":purge}
        except TurboError as exc:
            return failure(503,exc.code)
        except KeyError:
            return failure(404, "PROFILE_NOT_FOUND")
        except ProfileError:
            return failure(409, "PROFILE_ACTIVE")

    @app.post("/v1/audio/speech")
    async def speech(body: Speech, request: Request):
        try:
            script = parse_pause_script(body.input)
            parts = script.parts if script is not None else ()
            spoken_parts = [part.text for part in parts if hasattr(part, "text")] or [body.input]
            for spoken in spoken_parts:
                segment_text(spoken, body.seed)
        except PauseError as exc:
            return failure(422, exc.code)
        except SegmentationError as exc:
            return failure(422, exc.code)
        except ValueError as exc:
            return failure(422, str(exc) if str(exc) == "UNSUPPORTED_PARALINGUISTIC_TAG" else "INVALID_TEXT")
        try:
            request_id, cancel_token = request_cancel_identity(request)
        except ValueError as exc:
            return failure(422, str(exc))
        if not app.state.cache_trusted or not app.state.settings_consistent:
            return failure(503,"SERVICE_RECOVERY_REQUIRED")
        admission_owner = app.state.admission.try_acquire()
        if admission_owner is None or app.state.service.lock.locked():
            if admission_owner is not None:
                app.state.admission.release(admission_owner)
            response = failure(429,"WORKER_BUSY")
            response.headers["Retry-After"] = "1"
            return response
        active = None
        try:
            row = app.state.store.get(body.voice)
            if not row:
                return failure(404, "PROFILE_NOT_FOUND")
            if row["language"] != "en" or row.get("consent") != "confirmed":
                return failure(403, "VOICE_CONSENT_REQUIRED")
            try:
                reference = app.state.store.reference(row)
                with wave.open(str(reference), "rb") as w:
                    if w.getnframes() <= 5 * 24000:
                        return failure(422, "REFERENCE_TOO_SHORT")
            except (ProfileError, OSError, ValueError, wave.Error):
                return failure(409, "REFERENCE_INVALID")
            task = asyncio.create_task(app.state.service.synthesize(
                profile=row, reference=reference, text=body.input, script=script,
                speed=body.speed, seed=body.seed))
            active = {"marker": object(), "request_id": request_id,
                      "cancel_token": cancel_token, "task": task}
            app.state.active_generation = active
            try:
                # Disconnect must cancel and reap the worker, not detach generation.
                while not task.done():
                    if await request.is_disconnected():
                        task.cancel()
                        with contextlib.suppress(BaseException):
                            await task
                        return failure(499, "CLIENT_DISCONNECTED")
                    await asyncio.wait({task}, timeout=0.1)
                data, info = await task
                latest = app.state.store.get(body.voice)
                binding = reference_binding(row, body.voice)
                if (latest is None or reference_binding(latest, body.voice) != binding
                    or app.state.store.reference(latest).resolve(strict=True) != reference.resolve(strict=True)):
                    return failure(409, "REFERENCE_CHANGED")
                wav = wav_info(data, max_bytes=settings.max_output_bytes, max_seconds=600)
                if (wav["frames"] != info["audio_frames"]
                        or info["audio_frames"] != info["speech_frames"] + info["pause_frames"]):
                    return failure(500, "AUDIO_INTEGRITY_FAILURE")
                words = len(speech_text(script, body.input).split())
                speech_wpm = words * 60 / info["speech_s"]
                final_wpm = words * 60 / info["audio_s"]
                _receipt, _raw_receipt, receipt_token, proof = build_receipt(
                    profile=row, reference_binding=binding, input_text=body.input,
                    seed=body.seed, speed=body.speed, wav=data, info=info)
                requested_speed = speed_ascii(body.speed)
                meta = {"X-TTS-Engine": MODEL_ID, "X-TTS-Backend": "CUDA0",
                        "X-TTS-Model-Revision": MODEL_REVISION,
                        "X-TTS-Voice-Version": str(row["version"]),
                        "X-TTS-Elapsed-S": f'{info["elapsed_s"]:.6f}',
                        "X-TTS-Audio-S": frame_seconds(info["audio_frames"]),
                        "X-TTS-RTF": str(info["rtf"]), "X-TTS-Speed-Mode": "postprocess_atempo",
                        "X-TTS-Requested-Speed": requested_speed,
                        "X-TTS-Effective-Speed": requested_speed,
                        "X-TTS-Pause-Unaffected": "true", "X-TTS-Speech-WPM": f"{speech_wpm:.2f}",
                        "X-TTS-Final-WPM": f"{final_wpm:.2f}",
                        "X-Lite-Pause-Count": str(info["pause_count"]),
                        "X-Lite-Pause-S": frame_seconds(info["pause_frames"]),
                        "X-Lite-Speech-S": frame_seconds(info["speech_frames"]),
                        "X-Lite-Audio-S": frame_seconds(info["audio_frames"]),
                        "X-Lite-Speech-Segments": str(info["speech_segments"]),
                        "X-TTS-Native-Chunks": str(info["native_speech_segments"]),
                        "X-Lite-Speech-RTF": str(info["speech_rtf"]),
                        "X-TTS-Execution-Receipt": receipt_token,
                        "X-TTS-Execution-Proof": proof,
                        "X-TTS-Segmentation-Policy": info.get("seed_policy_version", POLICY_VERSION),
                        "Cache-Control": "no-store"}
                memory = info.get("memory") or {}
                for key, header in (
                    ("allocated_bytes", "X-TTS-CUDA-Allocated-Bytes"),
                    ("reserved_bytes", "X-TTS-CUDA-Reserved-Bytes"),
                    ("peak_bytes", "X-TTS-CUDA-Peak-Bytes"),
                    ("lifetime_peak_bytes", "X-TTS-CUDA-Lifetime-Peak-Bytes"),
                    ("free_bytes", "X-TTS-CUDA-Free-Bytes"),
                ):
                    if type(memory.get(key)) is int and memory[key] >= 0:
                        meta[header] = str(memory[key])
                if time.monotonic() >= info["deadline_at"]:
                    await app.state.service.stop()
                    return failure(504,"DEADLINE_EXCEEDED")
                if memory.get("peak_scope") in ("generation", "max_generation_in_request"):
                    meta["X-TTS-CUDA-Peak-Scope"] = memory["peak_scope"]
                return Response(data, media_type="audio/wav", headers=meta)
            except asyncio.CancelledError:
                return failure(499, "CANCELLED")
            except TurboError as exc:
                status = (503 if exc.code in ("ENV_NOT_INSTALLED", "CUDA_UNAVAILABLE",
                    "CUDA_LOW_MEMORY", "MODEL_MANIFEST_UNAVAILABLE", "MODEL_MANIFEST_INVALID", "MODEL_HASH_MISMATCH",
                    "MODEL_FILES_MISSING", "CUDA_OOM", "GGUF_PROBE_UNAVAILABLE") else 409 if exc.code == "GGUF_WORKER_RESIDENT" else 504 if exc.code == "DEADLINE_EXCEEDED"
                    else 422 if exc.code == "NANO_SEGMENT_SAFE_BOUNDARY_UNAVAILABLE"
                    else 409 if exc.code in ("REFERENCE_CHANGED", "REFERENCE_TOO_SHORT", "MODEL_IDENTITY_CHANGED") else 500)
                return failure(status, exc.code)
            except (AudioError, OSError, ValueError):
                return failure(500, "AUDIO_INTEGRITY_FAILURE")
            finally:
                if app.state.active_generation is active:
                    app.state.active_generation = None
        finally:
            app.state.admission.release(admission_owner)
    app.add_middleware(SizeGuard, limit=settings.max_ref_bytes + 65536)
    return app


app = create_app()
