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

MAGIC = 0x3150544C  # LTP1, little endian
VERSION = 1
HEADER = struct.Struct("<IHHII")
MAX_CONTROL = 64 * 1024
MAX_RESULT = 100 * 1024 * 1024
HELLO, LOAD, SYNTH, UNLOAD, SHUTDOWN = 1, 2, 3, 4, 5
CAPABILITY, READY, RESULT, OK, ERROR = 101, 102, 103, 104, 255
CAPABILITY_MARKER = b"LitePersistent/v1"
_CAPABILITY_CACHE: dict[tuple, dict] = {}

def persistent_command(exe: Path) -> list[str]:
    return [str(exe)]


def persistent_capability(settings: Settings) -> dict:
    """Verify the small receipt/executable only; never load/hash model weights."""
    receipt = settings.bin_dir / "persistent-capability.json"
    watched=(receipt,settings.bin_dir/"omnivoice-tts-windows-x86_64.exe",
             settings.bin_dir/"checksums.sha256",
             ROOT/"scripts/internal/stage-persistent-worker.py",
             ROOT/"scripts/internal/lite-persistent-worker.cpp")
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
                info.get("source_sha256") != source_digest):
            raise ValueError("receipt mismatch")
        result={"supported": True, "marker": "LitePersistent/v1",
                "protocol_version": 1, "exe_sha256": digest,
                "runtime_revision": info.get("runtime_revision")}
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
                "vram_status": "OWNER TEST PENDING"}

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

    async def _frame(self, command: int, payload: bytes = b"", *, limit=MAX_CONTROL):
        proc = self.process
        if not proc or proc.returncode is not None:
            raise NativeFailure("Persistent worker is not running")
        if len(payload) > (MAX_RESULT if command == SYNTH else MAX_CONTROL):
            raise NativeFailure("Persistent worker request exceeds bound")
        self._sequence += 1
        seq = self._sequence
        proc.stdin.write(HEADER.pack(MAGIC, VERSION, command, seq, len(payload)) + payload)
        try:
            await proc.stdin.drain()
            raw = await asyncio.wait_for(proc.stdout.readexactly(HEADER.size), self.settings.timeout)
            magic, version, reply, reply_seq, length = HEADER.unpack(raw)
            if magic != MAGIC or version != VERSION or reply_seq != seq or length > limit:
                raise NativeFailure("Persistent worker returned an invalid frame")
            body = await asyncio.wait_for(proc.stdout.readexactly(length), self.settings.timeout)
        except asyncio.TimeoutError as exc:
            raise NativeTimeout("Persistent worker command timed out") from exc
        except (asyncio.IncompleteReadError, BrokenPipeError, ConnectionResetError) as exc:
            raise NativeFailure("Persistent worker disconnected") from exc
        if reply == ERROR:
            raise NativeFailure("Persistent native operation failed")
        return reply, body

    async def _start(self):
        cap = persistent_capability(self.settings)
        if not cap["supported"]:
            raise RuntimeErrorLite("PERSISTENT_WORKER_UNAVAILABLE")
        state = await asyncio.to_thread(preflight, self.settings)
        if not state["ready"]:
            raise RuntimeErrorLite("Runtime is not ready")
        exe, _ = native_files(self.settings)
        base, codec = model_pair(self.settings)
        self.state = "LOADING"
        self.last_error = None
        self._stderr.clear()
        self.process = await asyncio.create_subprocess_exec(
            *persistent_command(exe), "--lite-worker-stdio", cwd=str(self.settings.bin_dir),
            env=native_env(self.settings), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=(os.name != "nt"),
            creationflags=(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0))
        self._stderr_task = asyncio.create_task(self._drain_stderr(self.process))
        try:
            reply, body = await self._frame(HELLO)
            if reply != CAPABILITY or body != CAPABILITY_MARKER:
                raise NativeFailure("Persistent worker handshake failed")
            reply, body = await self._frame(LOAD, _field(str(base).encode()) + _field(str(codec).encode()))
            if reply != READY or len(body) != 16:
                raise NativeFailure("Persistent worker load reply invalid")
            context, native_init, native_synth = struct.unpack("<QII", body)
            for _ in range(100):
                try:
                    proof = native_attestation(bytes(self._stderr), state["gpu"])
                    break
                except NativeFailure:
                    await asyncio.sleep(.01)
            else:
                raise NativeFailure("Loaded worker native proof missing")
            if native_init != 1 or native_synth != 0 or not context:
                raise NativeFailure("Persistent worker context counters invalid")
            self.context_identity = context
            self._native_synthesis_count = 0
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

    async def _synth(self, *, profile, reference, text, language, seed, speed):
        if not self.process or self.process.returncode is not None or self.state == "ERROR":
            await self._start()
        lang = (language or profile["language"]).strip().lower().encode()
        text_b, ref_text = text.encode(), profile["ref_text"].encode()
        pcm = await asyncio.to_thread(self._reference_pcm, reference)
        effective, _ = speed_to_native(speed)
        seed_value = secrets.randbits(31) if seed is None else seed
        payload = struct.pack("<If", seed_value, effective)
        payload += _field(text_b) + _field(lang) + _field(ref_text) + _field(pcm, 60 * 24000 * 2)
        self.state = "GENERATING"
        # RESULT has 24 bytes of metadata; the published PCM WAV adds a
        # canonical 44-byte header. Bound the complete eventual response.
        reply, body = await self._frame(SYNTH, payload, limit=self.settings.max_output_bytes - 20)
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
        out = io.BytesIO()
        with wave.open(out, "wb") as target:
            target.setnchannels(1); target.setsampwidth(2); target.setframerate(24000); target.writeframes(raw)
        self.state = "READY"
        return out.getvalue(), frames

    async def generate(self, *, profile, reference: Path, text: str, language, seed,
                       request_id: str, speed: float = 1.0):
        if self.lock.locked():
            raise NativeBusy("GPU is busy with another request")
        async with self.lock:
            self._cancel_idle(); self.active_id = profile["id"]; started = time.monotonic()
            try:
                data, frames = await self._synth(profile=profile, reference=reference, text=text,
                                                 language=language, seed=seed, speed=speed)
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
            try:
                for index, part in enumerate(p for p in script.parts if isinstance(p, SpeechPart)):
                    span_seed = None if seed is None else (seed + index) % 2147483648
                    data, _ = await self._synth(profile=profile, reference=reference, text=part.text,
                                                language=language, seed=span_seed, speed=speed)
                    segments.append(data)
                data, counts = assemble_pause_wav(script, segments, max_bytes=self.settings.max_output_bytes)
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
            self.state = "UNLOADED"; self.context_identity = None; return
        self.state = "UNLOADING"
        try:
            reply, body = await self._frame(UNLOAD)
            if reply != OK or body:
                raise NativeFailure("Persistent unload reply invalid")
            with contextlib.suppress(Exception):
                await self._frame(SHUTDOWN)
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
