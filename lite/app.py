"""Localhost-only, blocking OpenAI-compatible minimal GGUF Lite API."""
from __future__ import annotations
import asyncio
import contextlib
import logging
import uuid
import shutil
import math
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from .ui_routes import router as ui_router
from .ui_support import legacy_sources, migration_preview, migration_apply
from pydantic import BaseModel, Field, field_validator

from .config import Settings, REPO_ID, BASE_NAME, TOKENIZER_NAME
from .licence import Licence
from .native_runner import Runner, NativeBusy, NativeFailure, NativeTimeout
from .profiles import Store, ProfileError
from .runtime import preflight, RuntimeErrorLite
from .readiness import complete_readiness
from .speed import native_speed_capability, speed_to_native
from .pause import parse_pause_script, PauseError

logger = logging.getLogger("gguf_lite")

async def _owned_write(state, function, *args, **kwargs):
    """Nonqueued mutation admission, owned by its worker task, not HTTP caller.

    Even if Starlette cancels the HTTP request, the owned worker keeps the
    gate until FFmpeg/SQLite finishes, and no second request can observe a
    half-published voice. This does not retry or queue the operation.
    """
    if state.admission.locked() or state.runner.lock.locked():
        raise NativeBusy("Reference operation or GPU is active")
    await state.admission.acquire()
    async def owner():
        try:
            return await asyncio.to_thread(function,*args,**kwargs)
        finally:
            state.admission.release()
    try:
        task=asyncio.create_task(owner())
    except BaseException:
        state.admission.release()
        raise
    # Read exceptions even when the caller was cancelled; no private source log.
    def harvest(done):
        if not done.cancelled():
            done.exception()
    task.add_done_callback(harvest)
    return await asyncio.shield(task)

def failure(status: int, code: str, message: str, retry: bool = False):
    headers = {"Retry-After": "3"} if retry else None
    return JSONResponse(status_code=status, content={"error": {
        "message": message, "type": "lite_error", "code": code}}, headers=headers)

class BodyLimitExceeded(Exception):
    pass

class BoundedBodyASGI:
    """Enforce actual received bytes before Starlette's multipart spool parser."""
    def __init__(self,app,limit:int):
        self.app,self.limit=app,limit

    async def __call__(self,scope,receive,send):
        if scope["type"]!="http":
            return await self.app(scope,receive,send)
        length=next((value for name,value in scope.get("headers",[]) if name.lower()==b"content-length"),None)
        if length is not None:
            try:
                declared=int(length.decode("ascii"))
                if declared<0:
                    raise ValueError()
            except (UnicodeDecodeError,ValueError):
                return await failure(400,"BAD_LENGTH","Invalid Content-Length")(scope,receive,send)
            if declared>self.limit:
                return await failure(413,"BODY_TOO_LARGE","Request body exceeds limit")(scope,receive,send)
        received=0
        started=False
        async def bounded_receive():
            nonlocal received
            event=await receive()
            if event["type"]=="http.request":
                received+=len(event.get("body",b""))
                if received>self.limit:
                    raise BodyLimitExceeded()
            return event
        async def guarded_send(event):
            nonlocal started
            if event["type"]=="http.response.start":
                started=True
            await send(event)
        try:
            await self.app(scope,bounded_receive,guarded_send)
        except BodyLimitExceeded:
            if started:
                raise
            await failure(413,"BODY_TOO_LARGE","Request body exceeds limit")(scope,receive,send)

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
        if not math.isfinite(value) or not 0.75 <= value <= 1.25:
            raise ValueError("Native speed must be finite within 0.75–1.25")
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

class MigrationSelection(BaseModel):
    voice_id: str = Field(min_length=1,max_length=128)

    @field_validator("voice_id")
    @classmethod
    def safe_id(cls,value):
        import re
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}",value):
            raise ValueError("Invalid stable voice ID")
        return value

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
    # Admission ownership begins BEFORE mutable profile/reference resolution.
    # The runner retains its independent one-native-process lock.
    app.state.admission = asyncio.Lock()
    app.include_router(ui_router)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        peer=request.client.host if request.client else ""
        if peer not in ("127.0.0.1","::1","testclient"):
            return failure(403,"LOCAL_ONLY","Loopback clients only")
        host=request.headers.get("host","").lower()
        authority=host.split(":")[0] if not host.startswith("[") else host.split("]")[0]+"]"
        allowed=("127.0.0.1","localhost","[::1]")
        if authority not in allowed and not (peer=="testclient" and authority=="testserver"):
            return failure(403,"UNTRUSTED_HOST","Untrusted Host authority")
        if host.endswith(":"):
            return failure(400,"INVALID_HOST","Malformed Host")
        mutating=request.method in ("POST","PUT","PATCH","DELETE")
        if mutating:
            origin=request.headers.get("origin")
            site=request.headers.get("sec-fetch-site","").lower()
            if site not in ("","none","same-origin"):
                return failure(403,"CROSS_SITE_BLOCKED","Cross-site mutation forbidden")
            if origin:
                try:
                    parsed=urlsplit(origin)
                    if parsed.scheme!="http" or parsed.netloc.lower()!=host or parsed.path or parsed.query:
                        return failure(403,"CROSS_ORIGIN_BLOCKED","Cross-origin mutation forbidden")
                except ValueError:
                    return failure(403,"CROSS_ORIGIN_BLOCKED","Invalid Origin")
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, _exc):
        return failure(422, "INVALID_REQUEST", "Request failed validation")

    @app.get("/health")
    def health():
        return {"status": "alive", "ready": False}

    @app.get("/lite/readiness")
    async def readiness():
        engine = await asyncio.to_thread(preflight, settings)
        status = app.state.licence.status()
        state = complete_readiness(engine,status,store_healthy=settings.data_dir.is_dir(),
                                   ffmpeg_installed=bool(shutil.which("ffmpeg")))
        state["busy"] = app.state.runner.lock.locked() or app.state.admission.locked()
        return state

    @app.get("/engines/omnivoice-gguf/quants")
    async def quants():
        state = await asyncio.to_thread(preflight, settings)
        return {**state["quant"], "base": BASE_NAME, "tokenizer": TOKENIZER_NAME, "ready": state["ready"]}

    @app.get("/lite/status")
    def cheap_status():
        """No GPU preflight or multi-GB hash on this route."""
        native_busy=app.state.runner.lock.locked()
        operation_busy=app.state.admission.locked()
        return {"busy":native_busy or operation_busy,
                "native_busy":native_busy,"operation_busy":operation_busy,
                "voice_count":len(app.state.store.list()),"model":"omnivoice-gguf",
                "quant":"Q8_0","backend_requested":"Vulkan0",
                "ffmpeg_installed":bool(shutil.which("ffmpeg"))}

    @app.get("/lite/speed-capability")
    async def speed_capability():
        return await asyncio.to_thread(native_speed_capability,settings)

    @app.get("/lite/migration/preview")
    def migration_dry_run():
        try:
            db,voices=legacy_sources()
            return migration_preview(db,voices)
        except (ValueError,OSError,RuntimeError) as exc:
            return failure(409,"MIGRATION_SOURCE_UNAVAILABLE","Trusted legacy source not available")

    @app.post("/lite/migration/apply")
    async def migration_selected(payload:MigrationSelection):
        try:
            def selected_migration():
                db,voices=legacy_sources()
                return migration_apply(db,voices,settings,payload.voice_id)
            result=await _owned_write(app.state, selected_migration)
            if result["failed"]:
                return failure(409,"MIGRATION_FAILED","Migration failed; inspect local source and permissions")
            return result
        except NativeBusy:
            return failure(409,"PROFILE_ACTIVE","Native inference is active")
        except (ValueError,OSError,RuntimeError):
            return failure(409,"MIGRATION_FAILED","Legacy source changed or unavailable")

    @app.get("/v1/audio/voices")
    def voices():
        return [{"id": v["id"], "voice_id": v["id"], "name": v["name"],
                 "language": v["language"]} for v in app.state.store.list()]

    @app.get("/profiles")
    def profiles():
        return app.state.store.list()

    @app.get("/profiles/{profile_id}/reference")
    def reference_preview(profile_id:str):
        row=app.state.store.get(profile_id)
        if not row:
            return failure(404,"PROFILE_NOT_FOUND","Unknown voice ID")
        try:
            reference=app.state.store.reference(row)
            if reference.stat().st_size>settings.max_output_bytes:
                return failure(413,"REFERENCE_TOO_LARGE","Stored audio is too large")
            return Response(reference.read_bytes(),media_type="audio/wav",headers={
                "Cache-Control":"no-store","X-Content-Type-Options":"nosniff"})
        except (ProfileError,OSError):
            return failure(409,"REFERENCE_INVALID","Stored reference is invalid")

    @app.get("/profiles/{profile_id}")
    def get_profile(profile_id: str):
        row = app.state.store.get(profile_id)
        return row if row else failure(404, "PROFILE_NOT_FOUND", "Unknown voice ID")

    async def upload(name: str | None, ref_text: str | None, language: str | None, ref_audio: UploadFile,
                     profile_id: str | None = None, consent: str | None = None):
        if not ref_audio.filename:
            return failure(422, "AUDIO_REQUIRED", "WAV or MP3 is required")
        suffix = Path(ref_audio.filename).suffix.lower()
        payload = await ref_audio.read(settings.max_ref_bytes + 1)
        if len(payload) > settings.max_ref_bytes:
            return failure(413, "AUDIO_TOO_LARGE", "Reference is too large")
        if app.state.admission.locked() or app.state.runner.lock.locked():
            return failure(409,"PROFILE_ACTIVE","Native inference or mutation is active")
        try:
            def perform_import():
                actual_name,actual_text,actual_language=name,ref_text,language
                if profile_id is not None:
                    existing=app.state.store.get(profile_id)
                    if existing is None:
                        raise KeyError(profile_id)
                    actual_name,_,actual_language=(existing[k] for k in ("name","ref_text","language"))
                    # If ref_text was omitted, retain old behavior for legacy API
                    # clients; a new UI replacement must supply transcript B or
                    # explicitly confirm the recording says the same words.
                    actual_text=existing["ref_text"] if ref_text is None else ref_text
                return app.state.store.import_audio(name=actual_name,ref_text=actual_text,
                    language=actual_language,source=payload,suffix=suffix,
                    profile_id=profile_id,consent=consent)
            return await _owned_write(app.state,perform_import)
        except NativeBusy:
            return failure(409,"PROFILE_ACTIVE","Native inference or mutation is active")
        except KeyError:
            return failure(404, "PROFILE_NOT_FOUND", "Unknown voice ID")
        except (ValueError, OSError) as exc:
            return failure(422, "INVALID_AUDIO", str(exc) if isinstance(exc, ProfileError) else "Invalid reference audio")

    @app.post("/profiles")
    async def create_profile(name: str = Form(...), ref_text: str = Form(...),
                             language: str = Form("en"), ref_audio: UploadFile = File(...),
                             consent: str | None = Form(None)):
        return await upload(name, ref_text, language, ref_audio,consent=consent)

    @app.put("/profiles/{profile_id}/audio")
    async def replace_audio(profile_id: str, ref_audio: UploadFile = File(...),
                            consent: str | None = Form(None),
                            ref_text: str | None = Form(None),
                            reuse_existing_transcript: bool = Form(False)):
        # Omitting ref_text is a legacy compatibility path, not evidence that
        # the new audio contains the old words. UI never uses it silently.
        if ref_text is not None and (not ref_text.strip() or len(ref_text)>4096):
            return failure(422,"INVALID_TRANSCRIPT","Exact new transcript must be nonblank and at most 4096 characters")
        if reuse_existing_transcript and ref_text is not None:
            return failure(422,"INVALID_TRANSCRIPT","Choose either a new transcript or same-words confirmation")
        return await upload(None, ref_text, None, ref_audio, profile_id, consent=consent)

    @app.put("/profiles/{profile_id}")
    async def update_profile(profile_id: str, patch: ProfilePatch):
        if app.state.admission.locked() or app.state.runner.lock.locked():
            return failure(409,"PROFILE_ACTIVE","Native inference is active")
        try:
            return await _owned_write(app.state,app.state.store.update,profile_id,
                patch.model_dump(exclude_unset=True))
        except NativeBusy:
            return failure(409,"PROFILE_ACTIVE","Native inference or mutation is active")
        except KeyError:
            return failure(404, "PROFILE_NOT_FOUND", "Unknown voice ID")
        except ProfileError as exc:
            return failure(422, "INVALID_PROFILE", str(exc))

    @app.delete("/profiles/{profile_id}")
    async def delete_profile(profile_id: str, purge: bool = False):
        if app.state.admission.locked() or app.state.runner.lock.locked():
            return failure(409,"PROFILE_ACTIVE","Native inference is active")
        try:
            await _owned_write(app.state,app.state.store.delete,profile_id,
                active_id=app.state.runner.active_id,purge=purge)
        except NativeBusy:
            return failure(409,"PROFILE_ACTIVE","Native inference or mutation is active")
        except KeyError:
            return failure(404, "PROFILE_NOT_FOUND", "Unknown voice ID")
        except ProfileError as exc:
            if "cleanup" in str(exc):
                return failure(409,"PURGE_INCOMPLETE",
                    "Profile was removed, but owned audio cleanup requires local review")
            return failure(409, "PROFILE_ACTIVE", "Profile is active or the owned path is unsafe")
        return {"deleted": True, "id": profile_id, "owned_reference_files_purged":purge}

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
    async def speech(payload: Speech, request: Request):
        try:
            pause_script=parse_pause_script(payload.input)
        except PauseError as exc:
            return failure(422,exc.code,"Invalid or excessive inline pause script")
        # Non-queuing admission: once acquired, profile row/reference remain
        # stable until native success, failure, disconnect or termination.
        if app.state.admission.locked() or app.state.runner.lock.locked():
            return failure(429,"NATIVE_BUSY","One native inference already active",retry=True)
        async with app.state.admission:
            return await _speech_admitted(payload,request,pause_script)

    async def _speech_admitted(payload:Speech,request:Request,pause_script=None):
        state = app.state
        if state.runner.lock.locked():
            return failure(429, "NATIVE_BUSY", "One native inference already active", retry=True)
        row = state.store.get(payload.voice)
        if not row:
            return failure(404, "PROFILE_NOT_FOUND", "Unknown voice ID")
        if not state.licence.is_ready():
            return failure(403, "LICENCE_NOT_READY", "Model terms require verified provenance and explicit acceptance")
        effective_speed, speed_arg=speed_to_native(payload.speed)
        if payload.speed!=1.0:
            capability=await asyncio.to_thread(native_speed_capability,settings)
            if not capability["supported"]:
                return failure(503,"NATIVE_SPEED_UNAVAILABLE","Native Speed requires rebuilt Lite Vulkan runtime")
        try:
            reference = state.store.reference(row)
        except ProfileError:
            return failure(409, "REFERENCE_INVALID", "Stored reference is invalid")
        try:
            async def detect_disconnect():
                while True:
                    if await request.is_disconnected():
                        return
                    await asyncio.sleep(0.1)
            kwargs=dict(profile=row,reference=reference,text=payload.input,
                        language=payload.language,seed=payload.seed,
                        request_id=str(uuid.uuid4()))
            # Default speed preserves every old runner/provider call signature.
            if payload.speed!=1.0: kwargs["speed"]=effective_speed
            if pause_script is None:
                generation=asyncio.create_task(state.runner.generate(**kwargs))
            else:
                kwargs.pop("text")
                kwargs["script"]=pause_script
                generation=asyncio.create_task(state.runner.generate_paused(**kwargs))
            watcher=asyncio.create_task(detect_disconnect())
            try:
                done,_=await asyncio.wait({generation,watcher},
                    return_when=asyncio.FIRST_COMPLETED)
                if watcher in done and generation not in done:
                    generation.cancel()
                    with contextlib.suppress(BaseException):
                        await generation
                    return failure(499,"CLIENT_DISCONNECTED","Native generation cancelled")
                data,info=await generation
                # Defense in depth: never publish a WAV on a proofless or conflicting runner result.
                if (info.get("verification_status")!="verified" or
                    info.get("observed_native_backend")!="Vulkan0" or
                    "NVIDIA" not in info.get("observed_device_identity","") or
                    "GTX 1650" not in info.get("observed_device_identity","")):
                    return failure(500,"NATIVE_PROOF_MISSING","Native GPU proof was not verified")
                pause_headers={}
                if pause_script is not None:
                    expected={"pause_count":pause_script.pause_count,
                              "pause_frames":pause_script.pause_frames,
                              "speech_segments":pause_script.speech_segments}
                    if any(info.get(k)!=v for k,v in expected.items()):
                        return failure(500,"PAUSE_ASSEMBLY_FAILED","Pause metadata mismatch")
                    audio_frames=info.get("audio_frames")
                    speech_frames=info.get("speech_frames")
                    if (not isinstance(audio_frames,int) or
                        not isinstance(speech_frames,int) or
                        speech_frames<=0 or
                        audio_frames!=speech_frames+pause_script.pause_frames):
                        return failure(500,"PAUSE_ASSEMBLY_FAILED","Pause frame mismatch")
                    pause_headers={
                        "X-Lite-Pause-Count":str(pause_script.pause_count),
                        "X-Lite-Pause-S":str(pause_script.pause_frames/24000),
                        "X-Lite-Speech-S":str(speech_frames/24000),
                        "X-Lite-Audio-S":str(audio_frames/24000),
                        "X-Lite-Speech-Segments":str(pause_script.speech_segments),
                        "X-Lite-Speech-RTF":str(info.get("speech_rtf","")),
                    }
                return Response(data, media_type="audio/wav", headers={
                    **pause_headers,
                    "X-Lite-Request-Id": info["request_id"], "Cache-Control": "no-store",
                    "X-Lite-Requested-Speed":repr(payload.speed),
                    "X-Lite-Effective-Speed":speed_arg,
                    **({"X-Lite-Verified-Backend":info["observed_native_backend"],
                        "X-Lite-Observed-Device":info["observed_device_identity"],
                        "X-Lite-Verification-Status":info["verification_status"],
                        **({"X-Lite-Elapsed-S":str(info["elapsed_s"]),
                            "X-Lite-Audio-S":str(info["audio_s"]),
                            "X-Lite-RTF":str(info["rtf"])}
                           if all(k in info for k in ("elapsed_s","audio_s","rtf")) else {})}
                       if info.get("verification_status")=="verified" else {})})
            finally:
                watcher.cancel()
                if not generation.done():
                    generation.cancel()
                    with contextlib.suppress(BaseException):
                        await generation
                with contextlib.suppress(BaseException):
                    await watcher
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
    app.add_middleware(BoundedBodyASGI,limit=settings.max_ref_bytes+65536)
    return app

# Lazy: no native model load, no network and no heavy ML modules.
app = create_app()
