"""Persistent one-context native worker supervisor over bounded private stdio."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import os
import signal
import secrets
import struct
import subprocess
import time
import wave
from pathlib import Path

from .config import Settings, ROOT, RUNTIME_REVISION
from .native_runner import NativeBusy, NativeFailure, NativeTimeout, native_attestation
from .pause import SpeechPart
from .pause_audio import assemble_pause_wav
from .runtime import model_pair, native_env, native_files, preflight, RuntimeErrorLite
from .speed import speed_to_native
from .performance_metrics import parse_performance, aggregate_performance

MAGIC = 0x3150544C  # LTP1, little endian
VERSION = 1
HEADER = struct.Struct("<IHHII")
MAX_CONTROL = 64 * 1024
MAX_RESULT = 100 * 1024 * 1024
HELLO, LOAD, SYNTH, UNLOAD, SHUTDOWN, GET_PERF = 1, 2, 3, 4, 5, 6
CAPABILITY, READY, RESULT, OK, PERF, ERROR = 101, 102, 103, 104, 105, 255
CAPABILITY_MARKER = b"LitePersistent/v1"
TRUSTED_BASELINE_WORKER_SOURCES = {
    # Exact pre-performance worker source blobs (LF and Windows CRLF checkout).
    "22d56ecf993033e79f57d1daa59ecfa7c2ef11fd7d03b88232199e1ead067290",
    "9985f49c8650661e70e6e5ce4057b50e0ed63fc03a7c39cce21a958fe7f18e70",
}
_CAPABILITY_CACHE: dict[tuple, dict] = {}


def remaining(deadline: float) -> float:
    """Return the one-operation budget without ever refreshing it."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise NativeTimeout("Persistent generation deadline exceeded")
    return left

def persistent_command(exe: Path) -> list[str]:
    return [str(exe)]


def persistent_capability(settings: Settings) -> dict:
    """Verify the small receipt/executable only; never load/hash model weights."""
    receipt = settings.bin_dir / "persistent-capability.json"
    watched=(receipt,settings.bin_dir/"omnivoice-tts-windows-x86_64.exe",
             settings.bin_dir/"checksums.sha256",
             ROOT/"scripts/internal/stage-persistent-worker.py",
             ROOT/"scripts/internal/lite-persistent-worker.cpp")
    performance_receipt=settings.bin_dir/"performance-candidate.json"
    if performance_receipt.exists():
        watched += (performance_receipt, ROOT/"scripts/internal/instrument-native-performance.py",
                    ROOT/"scripts/internal/lite-native-perf.h",
                    ROOT/"scripts/internal/instrument-ref-conditioning-cache.py",
                    ROOT/"scripts/internal/lite-reference-cache.h",
                    ROOT/"scripts/internal/instrument-maskgit-experiments.py",
                    ROOT/"scripts/internal/lite-maskgit-experiments.h")
    try:
        signature=tuple((str(path),path.stat().st_mtime_ns,path.stat().st_size) for path in watched)
        key=(str(settings.bin_dir),signature)
        if key in _CAPABILITY_CACHE:
            return dict(_CAPABILITY_CACHE[key])
        info = json.loads(receipt.read_text(encoding="utf-8"))
        exe, _ = native_files(settings)
        digest = hashlib.sha256(exe.read_bytes()).hexdigest()
        patch_digest=hashlib.sha256((ROOT/"scripts/internal/stage-persistent-worker.py").read_bytes()).hexdigest()
        source_digest=hashlib.sha256((ROOT/"scripts/internal/lite-persistent-worker.cpp").read_bytes()).hexdigest()
        if (info.get("schema") != "lite-persistent-v1" or
                info.get("marker") != "LitePersistent/v1" or
                info.get("protocol_version") != 1 or
                info.get("runtime_revision") != RUNTIME_REVISION or
                info.get("exe_sha256") != digest or
                info.get("patch_sha256") != patch_digest or
                info.get("source_sha256") not in ({source_digest} | TRUSTED_BASELINE_WORKER_SOURCES)):
            raise ValueError("receipt mismatch")
        performance_protocol = info.get("source_sha256") == source_digest
        if performance_protocol:
            candidate=json.loads((settings.bin_dir/"performance-candidate.json").read_text(encoding="utf-8"))
            sources={
                "perf_patch_sha256": "instrument-native-performance.py",
                "perf_header_sha256": "lite-native-perf.h",
                "cache_patch_sha256": "instrument-ref-conditioning-cache.py",
                "cache_header_sha256": "lite-reference-cache.h",
                "maskgit_patch_sha256": "instrument-maskgit-experiments.py",
                "maskgit_header_sha256": "lite-maskgit-experiments.h",
            }
            if (candidate.get("schema")!="lite-performance-candidate-v1" or
                    candidate.get("tag")!="q8-vulkan-perf-opt-in" or
                    candidate.get("candidate_flavor") not in {"profiled","ref-cache","steps-32","steps-24","steps-16","steps-12","steps-8","fa-off"} or
                    candidate.get("exe_sha256")!=digest or candidate.get("baseline_quant")!="Q8_0" or
                    candidate.get("baseline_backend")!="Vulkan0" or candidate.get("default_ref_cache") is not False or
                    candidate.get("default_maskgit_steps")!=32 or candidate.get("graph_reuse")!="GRAPH_REUSE_DEFERRED"):
                raise ValueError("performance receipt mismatch")
            for field,name in sources.items():
                if candidate.get(field)!=hashlib.sha256((ROOT/"scripts/internal"/name).read_bytes()).hexdigest():
                    raise ValueError("performance source mismatch")
        result={"supported": True, "marker": "LitePersistent/v1",
                "protocol_version": 1, "exe_sha256": digest,
                "runtime_revision": info.get("runtime_revision"),
                "performance_protocol": performance_protocol}
        _CAPABILITY_CACHE.clear();_CAPABILITY_CACHE[key]=result
        return dict(result)
    except (OSError, ValueError, TypeError, json.JSONDecodeError, RuntimeErrorLite) as exc:
        return {"supported": False, "marker": "LitePersistent/v1",
                "protocol_version": 1, "reason": "verified persistent worker unavailable"}


def _field(value: bytes, limit: int = MAX_CONTROL) -> bytes:
    if len(value) > limit:
        raise NativeFailure("Persistent worker field exceeds bound")
    return struct.pack("<I", len(value)) + value


class ResidentWorkerSupervisor:
    def __init__(self, settings: Settings, settings_provider):
        self.settings = settings
        self.settings_provider = settings_provider
        self.lock = asyncio.Lock()
        self.active_id: str | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.state = "UNLOADED"
        self.generation = 0
        self.cold_load_count = 0
        self.synthesis_count = 0
        self._native_synthesis_count = 0
        self.reuse_count = 0
        self.context_identity: int | None = None
        self.last_error: str | None = None
        self._sequence = 0
        self._stderr = bytearray()
        self._stderr_task: asyncio.Task | None = None
        self._idle_task: asyncio.Task | None = None
        self._idle_deadline: float | None = None
        self._idle_token = 0
        self._verified_models: tuple[Path, Path] | None = None
        self._model_file_identity: tuple | None = None
        self._perf_enabled = os.environ.get("GGUF_LITE_PERF", "0") == "1"
        self._perf_supported = False
        self.last_performance: dict | None = None
        self._last_native_perf: dict | None = None

    async def _await_deadline(self, awaitable, deadline: float):
        try:
            budget = remaining(deadline)
        except BaseException:
            close = getattr(awaitable, "close", None)
            if close:
                close()
            raise
        try:
            async with asyncio.timeout(budget):
                return await awaitable
        except TimeoutError as exc:
            raise NativeTimeout("Persistent generation deadline exceeded") from exc

    def status(self) -> dict:
        busy = self.lock.locked() or self.state in {"LOADING", "GENERATING", "UNLOADING"}
        remaining = None
        if not busy and self._idle_deadline is not None and self.state == "READY":
            remaining = max(0, int(self._idle_deadline - time.monotonic() + .999))
        return {"state": self.state, "resident": self.state in {"READY", "GENERATING"},
                "pid": self.process.pid if self.process and self.process.returncode is None else None,
                "worker_generation": self.generation,
                "context_identity_verified": self.context_identity is not None,
                "cold_load_count": self.cold_load_count, "synthesis_count": self.synthesis_count,
                "reuse_count": self.reuse_count, "idle_remaining_seconds": remaining,
                "busy": busy, "last_error": self.last_error,
                "vram_status": "OWNER TEST PENDING",
                "performance": self.last_performance if self._perf_enabled else None,
                 "native_proof": getattr(self, "_proof", None) if self.state in {"READY", "GENERATING"} else None}

    async def _drain_stderr(self, proc):
        try:
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    return
                if len(self._stderr) + len(chunk) > 131072:
                    raise NativeFailure("Persistent worker stderr exceeded 128 KiB")
                self._stderr.extend(chunk)
        except BaseException:
            if proc.returncode is None:
                await self._terminate(proc)

    async def _terminate(self, proc):
        if proc.returncode is not None:
            return
        if os.name == "nt":
            with contextlib.suppress(Exception):
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/T", "/F", "/PID", str(proc.pid),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await asyncio.wait_for(killer.wait(), 8)
        else:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 10)

    async def _frame(self, command: int, payload: bytes = b"", *, deadline: float,
                     limit=MAX_CONTROL):
        proc = self.process
        if not proc or proc.returncode is not None:
            raise NativeFailure("Persistent worker is not running")
        if len(payload) > (MAX_RESULT if command == SYNTH else MAX_CONTROL):
            raise NativeFailure("Persistent worker request exceeds bound")
        self._sequence += 1
        seq = self._sequence
        proc.stdin.write(HEADER.pack(MAGIC, VERSION, command, seq, len(payload)) + payload)
        try:
            await self._await_deadline(proc.stdin.drain(), deadline)
            raw = await self._await_deadline(proc.stdout.readexactly(HEADER.size), deadline)
            magic, version, reply, reply_seq, length = HEADER.unpack(raw)
            if magic != MAGIC or version != VERSION or reply_seq != seq or length > limit:
                raise NativeFailure("Persistent worker returned an invalid frame")
            body = await self._await_deadline(proc.stdout.readexactly(length), deadline)
        except (asyncio.IncompleteReadError, BrokenPipeError, ConnectionResetError) as exc:
            raise NativeFailure("Persistent worker disconnected") from exc
        if reply == ERROR:
            raise NativeFailure("Persistent native operation failed")
        return reply, body

    async def _start(self, deadline: float):
        remaining(deadline)
        cap = await self._await_deadline(
            asyncio.to_thread(persistent_capability, self.settings), deadline)
        if not cap["supported"]:
            raise RuntimeErrorLite("PERSISTENT_WORKER_UNAVAILABLE")
        self._perf_supported = bool(cap.get("performance_protocol"))
        state = await self._await_deadline(asyncio.to_thread(preflight, self.settings), deadline)
        if not state["ready"]:
            raise RuntimeErrorLite("Runtime is not ready")
        exe, _ = await self._await_deadline(
            asyncio.to_thread(native_files, self.settings), deadline)
        models = await self._await_deadline(
            asyncio.to_thread(model_pair, self.settings), deadline)
        model_identity = await self._await_deadline(
            asyncio.to_thread(self._model_identity, models), deadline)
        base, codec = models
        self.state = "LOADING"
        self.last_error = None
        self._stderr.clear()
        self.process = await self._await_deadline(asyncio.create_subprocess_exec(
            *persistent_command(exe), "--lite-worker-stdio", cwd=str(self.settings.bin_dir),
            env=native_env(self.settings), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=(os.name != "nt"),
            creationflags=(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0)), deadline)
        self._stderr_task = asyncio.create_task(self._drain_stderr(self.process))
        try:
            reply, body = await self._frame(HELLO, deadline=deadline)
            if reply != CAPABILITY or body != CAPABILITY_MARKER:
                raise NativeFailure("Persistent worker handshake failed")
            reply, body = await self._frame(
                LOAD, _field(str(base).encode()) + _field(str(codec).encode()), deadline=deadline)
            if reply != READY or len(body) != 16:
                raise NativeFailure("Persistent worker load reply invalid")
            context, native_init, native_synth = struct.unpack("<QII", body)
            for _ in range(100):
                try:
                    proof = native_attestation(bytes(self._stderr), state["gpu"])
                    break
                except NativeFailure:
                    await self._await_deadline(asyncio.sleep(.01), deadline)
            else:
                raise NativeFailure("Loaded worker native proof missing")
            if native_init != 1 or native_synth != 0 or not context:
                raise NativeFailure("Persistent worker context counters invalid")
            # Close the verify-to-load race: the paths sent to ov_init must
            # still pass the full pinned SHA check after LOAD completes.
            loaded_models = await self._await_deadline(
                asyncio.to_thread(model_pair, self.settings), deadline)
            loaded_identity = await self._await_deadline(
                asyncio.to_thread(self._model_identity, loaded_models), deadline)
            if loaded_models != models or loaded_identity != model_identity:
                raise NativeFailure("Pinned model changed during worker load")
            self.context_identity = context
            self._last_native_perf = None
            self.last_performance = None
            self._native_synthesis_count = 0
            self._verified_models = loaded_models
            self._model_file_identity = loaded_identity
            self.generation += 1
            self.cold_load_count += 1
            self._proof = proof
            self.state = "READY"
        except BaseException as exc:
            await self._fault(exc)
            raise

    async def _fault(self, exc):
        self.last_error = type(exc).__name__
        proc, self.process = self.process, None
        stderr_task, self._stderr_task = self._stderr_task, None
        self.state = "ERROR"
        self.context_identity = None
        self._verified_models = None
        self._model_file_identity = None
        self.last_performance = None
        self._last_native_perf = None
        self._cancel_idle()
        if proc:
            await self._terminate(proc)
        if stderr_task and stderr_task is not asyncio.current_task():
            with contextlib.suppress(BaseException):
                await stderr_task

    def _cancel_idle(self):
        self._idle_token += 1
        self._idle_deadline = None
        if (self._idle_task and not self._idle_task.done() and
                self._idle_task is not asyncio.current_task()):
            self._idle_task.cancel()
        self._idle_task = None

    def _arm_idle(self):
        self._cancel_idle()
        timeout = self.settings_provider().idle_timeout_seconds
        if timeout is None or self.state != "READY":
            return
        token = self._idle_token
        self._idle_deadline = time.monotonic() + timeout
        async def wait_then_unload():
            try:
                await asyncio.sleep(timeout)
                if token == self._idle_token and not self.lock.locked() and self.state == "READY":
                    async with self.lock:
                        if token == self._idle_token and self.state == "READY":
                            await self._unload_locked()
            except asyncio.CancelledError:
                pass
        self._idle_task = asyncio.create_task(wait_then_unload())

    @staticmethod
    def _reference_pcm(path: Path) -> bytes:
        with wave.open(str(path), "rb") as source:
            if source.getnchannels() != 1 or source.getsampwidth() != 2 or source.getframerate() != 24000:
                raise NativeFailure("Reference must be 24 kHz mono PCM16 WAV")
            if source.getnframes() <= 0 or source.getnframes() > 60 * 24000:
                raise NativeFailure("Reference duration violates bound")
            return source.readframes(source.getnframes())

    @staticmethod
    def _model_identity(models: tuple[Path, Path]) -> tuple:
        identity = []
        for path in models:
            resolved = path.resolve(strict=True)
            source_stat = path.lstat()
            target_stat = resolved.stat()
            identity.append((str(path), str(resolved), source_stat.st_dev, source_stat.st_ino,
                             source_stat.st_size, source_stat.st_mtime_ns, source_stat.st_ctime_ns,
                             target_stat.st_dev, target_stat.st_ino, target_stat.st_size,
                             target_stat.st_mtime_ns, target_stat.st_ctime_ns))
        return tuple(identity)

    async def _check_resident_models(self, deadline: float):
        if not self._verified_models or not self._model_file_identity:
            raise NativeFailure("Resident model identity is unavailable")
        current = await self._await_deadline(
            asyncio.to_thread(self._model_identity, self._verified_models), deadline)
        if current != self._model_file_identity:
            # A changed identity always invalidates the loaded context. Re-run
            # the full pinned SHA verification so a malicious/invalid change
            # cannot be treated as a benign cache refresh.
            await self._await_deadline(asyncio.to_thread(model_pair, self.settings), deadline)
            raise NativeFailure("Pinned model changed while worker was resident")

    @classmethod
    def _reference_identity(cls, reference: Path):
        pcm = cls._reference_pcm(reference)
        return str(reference.resolve(strict=True)), hashlib.sha256(pcm).digest()

    async def _reference_snapshot(self, reference: Path, deadline: float):
        return await self._await_deadline(
            asyncio.to_thread(self._reference_identity, reference), deadline)

    async def _prepare_operation(self, reference: Path, deadline: float):
        if not self.process or self.process.returncode is not None or self.state == "ERROR":
            await self._start(deadline)
        await self._check_resident_models(deadline)
        return await self._reference_snapshot(reference, deadline)

    async def _finish_operation(self, reference: Path, expected, deadline: float):
        await self._check_resident_models(deadline)
        if await self._reference_snapshot(reference, deadline) != expected:
            raise NativeFailure("Pinned reference changed during generation")

    async def _synth(self, *, profile, reference, text, language, seed, speed,
                     deadline: float):
        if not self.process or self.process.returncode is not None or self.state == "ERROR":
            await self._start(deadline)
        remaining(deadline)
        lang = (language or profile["language"]).strip().lower().encode()
        text_b, ref_text = text.encode(), profile["ref_text"].encode()
        pcm = await self._await_deadline(asyncio.to_thread(self._reference_pcm, reference), deadline)
        effective, _ = speed_to_native(speed)
        seed_value = secrets.randbits(31) if seed is None else seed
        payload = struct.pack("<If", seed_value, effective)
        payload += _field(text_b) + _field(lang) + _field(ref_text) + _field(pcm, 60 * 24000 * 2)
        self.state = "GENERATING"
        # RESULT has 24 bytes of metadata; the published PCM WAV adds a
        # canonical 44-byte header. Bound the complete eventual response.
        reply, body = await self._frame(
            SYNTH, payload, deadline=deadline, limit=self.settings.max_output_bytes - 20)
        if reply != RESULT or len(body) < 24:
            raise NativeFailure("Persistent worker result invalid")
        context, native_init, native_synth, rate, frames = struct.unpack("<QIIII", body[:24])
        raw = body[24:]
        if (context != self.context_identity or native_init != 1 or native_synth != self._native_synthesis_count + 1 or
                rate != 24000 or frames <= 0 or len(raw) != frames * 2 or
                len(raw) + 44 > self.settings.max_output_bytes):
            raise NativeFailure("Persistent worker result identity/counters invalid")
        if self._native_synthesis_count > 0:
            self.reuse_count += 1
        self.synthesis_count += 1
        self._native_synthesis_count += 1
        if self._perf_enabled and self._perf_supported:
            perf_reply, perf_body = await self._frame(GET_PERF, deadline=deadline, limit=MAX_CONTROL)
            if perf_reply != PERF:
                raise NativeFailure("Persistent worker performance reply invalid")
            try:
                self.last_performance = parse_performance(perf_body)
                self._last_native_perf = dict(self.last_performance)
            except ValueError as exc:
                raise NativeFailure("Persistent worker performance payload invalid") from exc
        out = io.BytesIO()
        with wave.open(out, "wb") as target:
            target.setnchannels(1); target.setsampwidth(2); target.setframerate(24000); target.writeframes(raw)
        remaining(deadline)
        self.state = "READY"
        return out.getvalue(), frames

    async def generate(self, *, profile, reference: Path, text: str, language, seed,
                       request_id: str, speed: float = 1.0):
        if self.lock.locked():
            raise NativeBusy("GPU is busy with another request")
        async with self.lock:
            self._cancel_idle(); self.active_id = profile["id"]; started = time.monotonic()
            deadline = started + self.settings.timeout
            try:
                integrity = await self._prepare_operation(reference, deadline)
                data, frames = await self._synth(profile=profile, reference=reference, text=text,
                                                 language=language, seed=seed, speed=speed,
                                                 deadline=deadline)
                await self._finish_operation(reference, integrity, deadline)
                remaining(deadline)
                elapsed = time.monotonic() - started
                return data, {"request_id": request_id, "profile_id": profile["id"], "quant": "Q8_0",
                              **self._proof, "elapsed_s": round(elapsed, 3), "audio_s": frames / 24000,
                              "rtf": round(elapsed / (frames / 24000), 3),
                              "worker_pid": self.process.pid, "worker_generation": self.generation}
            except BaseException as exc:
                await self._fault(exc)
                raise
            finally:
                self.active_id = None
                if self.state == "READY": self._arm_idle()

    async def generate_paused(self, *, script, profile, reference: Path, language, seed,
                              request_id: str, speed: float = 1.0):
        if self.lock.locked(): raise NativeBusy("GPU is busy with another request")
        async with self.lock:
            self._cancel_idle(); self.active_id = profile["id"]; started = time.monotonic(); segments = []
            deadline = started + self.settings.timeout
            try:
                integrity = await self._prepare_operation(reference, deadline)
                perf_prior = dict(self._last_native_perf) if self._last_native_perf else None
                perf_spans = []
                for index, part in enumerate(p for p in script.parts if isinstance(p, SpeechPart)):
                    span_seed = None if seed is None else (seed + index) % 2147483648
                    data, _ = await self._synth(profile=profile, reference=reference, text=part.text,
                                                language=language, seed=span_seed, speed=speed,
                                                deadline=deadline)
                    segments.append(data)
                    if self._perf_enabled and self._perf_supported:
                        if self._last_native_perf is None:
                            raise NativeFailure("Native performance snapshot missing")
                        perf_spans.append(dict(self._last_native_perf))
                remaining(deadline)
                data, counts = assemble_pause_wav(script, segments, max_bytes=self.settings.max_output_bytes)
                await self._finish_operation(reference, integrity, deadline)
                remaining(deadline)
                if self._perf_enabled and self._perf_supported:
                    try:
                        self.last_performance = aggregate_performance(perf_spans, perf_prior)
                    except ValueError as exc:
                        raise NativeFailure("Native multi-span performance invalid") from exc
                elapsed = time.monotonic() - started
                speech_s = counts["speech_frames"] / 24000
                total_s = counts["audio_frames"] / 24000
                return data, {"request_id": request_id, "profile_id": profile["id"], "quant": "Q8_0",
                              **self._proof, **counts, "elapsed_s": round(elapsed, 3), "audio_s": total_s,
                              "speech_s": speech_s, "pause_s": counts["pause_frames"] / 24000,
                              "rtf": round(elapsed / total_s, 3), "speech_rtf": round(elapsed / speech_s, 3),
                              "worker_pid": self.process.pid, "worker_generation": self.generation}
            except BaseException as exc:
                await self._fault(exc); raise
            finally:
                self.active_id = None
                if self.state == "READY": self._arm_idle()

    async def _unload_locked(self):
        self._cancel_idle()
        proc = self.process
        if not proc:
            self.state = "UNLOADED"; self.context_identity = None
            self._verified_models = None; self._model_file_identity = None
            self._last_native_perf = None; self.last_performance = None; return
        self.state = "UNLOADING"
        cleanup_deadline = time.monotonic() + 10
        try:
            reply, body = await self._frame(UNLOAD, deadline=cleanup_deadline)
            if reply != OK or body:
                raise NativeFailure("Persistent unload reply invalid")
            with contextlib.suppress(Exception):
                await self._frame(SHUTDOWN, deadline=cleanup_deadline)
            with contextlib.suppress(Exception):
                proc.stdin.close()
            await asyncio.wait_for(proc.wait(), 10)
        except BaseException:
            await self._terminate(proc)
        finally:
            stderr_task, self._stderr_task = self._stderr_task, None
            if stderr_task and stderr_task is not asyncio.current_task():
                with contextlib.suppress(BaseException):
                    await stderr_task
            self.process = None; self.context_identity = None; self.state = "UNLOADED"
            self._verified_models = None; self._model_file_identity = None
            self._last_native_perf = None; self.last_performance = None

    async def unload_now(self):
        if self.lock.locked(): raise NativeBusy("Worker is busy")
        async with self.lock: await self._unload_locked()

    async def stop(self):
        self._cancel_idle()
        if self.lock.locked():
            proc = self.process
            if proc: await self._terminate(proc)
            self.process = None; self.state = "UNLOADED"; return
        async with self.lock: await self._unload_locked()


class RuntimeRunner:
    """Single admission facade retaining the proven one-shot implementation."""
    def __init__(self, settings: Settings, legacy, settings_store):
        self.settings = settings
        self.legacy = legacy
        self.settings_store = settings_store
        self.persistent = ResidentWorkerSupervisor(settings, self.settings_store.load)
        self.lock = asyncio.Lock()
        self.active_id = None

    async def _run(self, method, kwargs):
        if self.lock.locked(): raise NativeBusy("GPU is busy with another request")
        async with self.lock:
            chosen = self.settings_store.load().mode
            target = self.persistent if chosen == "persistent" else self.legacy
            if target is self.persistent:
                # Admission won the race: invalidate the idle generation
                # synchronously before the supervisor lock can be acquired.
                self.persistent._cancel_idle()
            self.active_id = kwargs["profile"]["id"]
            try: return await getattr(target, method)(**kwargs)
            finally:
                self.active_id = None
                if self.settings_store.load().mode == "legacy_oneshot" and self.persistent.state == "READY":
                    await self.persistent.unload_now()

    async def generate(self, **kwargs): return await self._run("generate", kwargs)
    async def generate_paused(self, **kwargs): return await self._run("generate_paused", kwargs)
    async def stop(self):
        await self.legacy.stop(); await self.persistent.stop()
