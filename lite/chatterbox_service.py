"""One serial, deadline-bounded persistent worker. Never replay uncertain generation."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import errno
import os
import json
import urllib.request
import urllib.error
import sys
import time
from pathlib import Path

from .audio import AudioError, wav_info
from .chatterbox_audio import stretch_speech_async, concatenate
from .chatterbox_config import ChatterboxSettings, MODEL_REVISION, IDLE_CHOICES
from .chatterbox_segmentation import segment_text, SegmentationError, POLICY_VERSION
from .chatterbox_ipc import send, recv, ProtocolError
from .pause import PauseScript, SpeechPart, PausePart
from .pause_audio import assemble_pause_wav


# Only this loopback endpoint may authorize releasing the 4 GiB GPU interlock.
# Windows can report an absent listener as a delayed WinError 10061 (>2 s).
# Keep both the socket-level and coroutine deadlines finite and independent.
GGUF_STATUS_URL = "http://127.0.0.1:3900/lite/worker/status"
GGUF_HTTP_TIMEOUT_S = 4.5
GGUF_PROBE_OUTER_TIMEOUT_S = 5.5
GGUF_PROBE_GRACE_S = 0.5


def _connection_explicitly_refused(reason) -> bool:
    """Only a definite TCP refusal is proof of *no* GGUF listener.

    Timeouts, resets, remote HTTP failures and malformed JSON stay unknown.
    """
    return (isinstance(reason, ConnectionRefusedError)
            or getattr(reason, "errno", None) in (errno.ECONNREFUSED, 10061)
            or getattr(reason, "winerror", None) == 10061)


class TurboError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def finalize_audio(script, speech_wavs, max_output_bytes):
    """Pure bounded final assembly/validation. Called outside the event loop."""
    if script:
        data, meta = assemble_pause_wav(script, speech_wavs,
            max_bytes=max_output_bytes, max_seconds=600)
    else:
        data=speech_wavs[0]
        initial=wav_info(data,max_bytes=max_output_bytes,max_seconds=600)
        meta={"pause_count":0,"pause_frames":0,"speech_frames":initial["frames"],
              "audio_frames":initial["frames"],"speech_segments":1}
    info=wav_info(data,max_bytes=max_output_bytes,max_seconds=600)
    if info["frames"]!=meta["audio_frames"]:
        raise TurboError("AUDIO_INTEGRITY_FAILURE")
    return data,meta,info


class TurboService:
    def __init__(self, settings: ChatterboxSettings):
        self.settings = settings
        self.lock = asyncio.Lock()
        self.proc = None
        self.state = "UNLOADED"
        self.last_error = None
        self.idle_timeout = 600
        self.idle_deadline = None
        self.idle_task = None
        self.active_id = None
        self.loads = 0
        self.syntheses = 0
        self.hits = 0
        self.device = None
        self.memory = None
        self.model_hash_verified = False
        self.live_generation_verified_at = None

    def status(self):
        resident = bool(self.proc and self.proc.returncode is None)
        return {"state": self.state, "resident": resident,
                "pid": self.proc.pid if self.proc and self.proc.returncode is None else None,
                "busy": self.lock.locked(), "active": self.active_id is not None,
                "load_count": self.loads, "generation_count": self.syntheses,
                "condition_cache_hits": self.hits, "last_error": self.last_error,
                "cuda": self.device if self.device is not None else "NOT_MEASURED",
                "memory": self.memory if self.memory is not None else {"state": "NOT_MEASURED"},
                "idle_remaining_seconds": max(0, self.idle_deadline - time.monotonic())
                    if self.idle_deadline is not None else None,
                "model": "chatterbox-nano", "backend": "CUDA0",
                "idle_timeout_seconds": self.idle_timeout,
                "model_revision": MODEL_REVISION,
                "model_hash_verified": bool(self.model_hash_verified and resident),
                "cuda_verified": bool(isinstance(self.device,dict)
                    and self.device.get("evidence") == "runtime_cuda_probe"
                    and resident),
                "live_generation_verified": bool(
                    self.live_generation_verified_at is not None and resident),
                "live_generation_verified_at": (
                    self.live_generation_verified_at if resident else None)}

    def _cancel_idle(self):
        self.idle_deadline = None
        if self.idle_task:
            prior, self.idle_task = self.idle_task, None
            try:
                running = asyncio.current_task()
            except RuntimeError:
                running = None
            if prior is not running:
                prior.cancel()

    def _arm_idle(self):
        self._cancel_idle()
        if self.idle_timeout is None or self.state != "READY":
            return
        self.idle_deadline = time.monotonic() + self.idle_timeout
        async def later():
            try:
                await asyncio.sleep(self.idle_timeout)
                if not self.lock.locked():
                    await self.unload_now()
            except asyncio.CancelledError:
                return
        self.idle_task = asyncio.create_task(later())

    def set_idle(self, seconds):
        if seconds not in IDLE_CHOICES:
            raise TurboError("INVALID_IDLE_TIMEOUT")
        self.idle_timeout = seconds
        if self.state == "READY" and not self.lock.locked():
            self._arm_idle()

    async def _kill(self):
        self._cancel_idle()
        proc, self.proc = self.proc, None
        try:
            if proc and proc.returncode is None:
                self.state = "UNLOADING"
                if os.name=="nt":
                    # Python may have spawned CUDA/worker descendants. Kill the job tree.
                    import subprocess
                    try:
                        await asyncio.to_thread(subprocess.run,
                            ["taskkill","/PID",str(proc.pid),"/T","/F"],
                            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                            timeout=5,check=False)
                    except (OSError,subprocess.TimeoutExpired):
                        pass
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.device = None
            self.memory = None
            self.model_hash_verified = False
            self.live_generation_verified_at = None
            self.state = "UNLOADED"
            self.active_id = None

    async def stop(self):
        await self._kill()

    async def unload_now(self):
        if self.lock.locked():
            raise TurboError("WORKER_BUSY")
        await self._kill()
        return {"unloaded": True, "state": "UNLOADED"}

    async def _rpc(self, message: dict, deadline: float):
        if self.proc is None or self.proc.returncode is not None:
            raise TurboError("WORKER_CRASH")
        left = deadline - time.monotonic()
        if left <= 0:
            raise TurboError("DEADLINE_EXCEEDED")
        try:
            await asyncio.wait_for(send(self.proc.stdin, message), left)
            result = await asyncio.wait_for(recv(self.proc.stdout), max(0.001, deadline - time.monotonic()))
        except asyncio.TimeoutError as exc:
            raise TurboError("DEADLINE_EXCEEDED") from exc
        except ProtocolError as exc:
            raise TurboError("WORKER_PROTOCOL_INVALID") from exc
        except (OSError, EOFError, asyncio.IncompleteReadError, BrokenPipeError) as exc:
            raise TurboError("WORKER_CRASH") from exc
        if not result.get("ok"):
            raise TurboError(result.get("error", "WORKER_FAILURE"))
        return result.get("result", {})

    async def _assert_no_gguf(self, deadline: float) -> None:
        """Read-only GPU isolation interlock with Windows-aware bounded deadlines.

        Explicit TCP refusal means no listener. Any unverified response must
        fail closed; timeouts never imply that GPU memory is available.
        """
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TurboError("DEADLINE_EXCEEDED")
        outer_budget = min(GGUF_PROBE_OUTER_TIMEOUT_S, remaining)
        # urlopen has its own socket timeout. Give it enough time to observe
        # delayed Windows refusal, but leave grace for parsing/thread dispatch.
        http_budget = min(GGUF_HTTP_TIMEOUT_S,
                          max(0.05, outer_budget - GGUF_PROBE_GRACE_S))

        def probe():
            request = urllib.request.Request(
                GGUF_STATUS_URL, headers={"Accept": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=http_budget) as result:
                    if result.status != 200:
                        raise ValueError("GGUF status unavailable")
                    data = result.read(16385)
                    if len(data) > 16384:
                        raise ValueError("Invalid GGUF status")
                    obj = json.loads(data)
                    if (not isinstance(obj, dict)
                        or type(obj.get("resident")) is not bool
                        or type(obj.get("busy")) is not bool):
                        raise ValueError("Unknown GGUF status schema")
                    return obj["resident"] or obj["busy"]
            except urllib.error.URLError as exc:
                if _connection_explicitly_refused(exc.reason):
                    return False
                raise
            except OSError as exc:
                if _connection_explicitly_refused(exc):
                    return False
                raise

        try:
            conflict = await asyncio.wait_for(
                asyncio.to_thread(probe), timeout=outer_budget)
        except asyncio.TimeoutError as exc:
            if time.monotonic() >= deadline:
                raise TurboError("DEADLINE_EXCEEDED") from exc
            raise TurboError("GGUF_PROBE_UNAVAILABLE") from exc
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise TurboError("GGUF_PROBE_UNAVAILABLE") from exc
        # asyncio.CancelledError must propagate; no CUDA load or CPU fallback.
        if time.monotonic() >= deadline:
            raise TurboError("DEADLINE_EXCEEDED")
        if conflict:
            raise TurboError("GGUF_WORKER_RESIDENT")

    async def _ensure_loaded(self, deadline):
        self._cancel_idle()
        if self.proc and self.proc.returncode is None and self.state == "READY":
            return
        await self._assert_no_gguf(deadline)
        self.state = "LOADING"
        python = self.settings.venv_python
        if not python.is_file():
            raise TurboError("ENV_NOT_INSTALLED")
        env = dict(os.environ)
        root = Path(__file__).resolve().parents[1]
        env["CHATTERBOX_ROOT"] = str(self.settings.root)
        env["PYTHONPATH"] = str(root)
        for variable, sub in (("HF_HOME", "hf"), ("HF_HUB_CACHE", "hf/hub"),
                              ("TORCH_HOME", "torch"), ("TMP", "tmp"), ("TEMP", "tmp")):
            path = self.settings.cache_dir / sub
            path.mkdir(parents=True, exist_ok=True)
            env[variable] = str(path)
        try:
            self.proc = await asyncio.wait_for(asyncio.create_subprocess_exec(
                str(python), "-m", "lite.chatterbox_worker",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, cwd=str(root), env=env),
                max(0.001, deadline-time.monotonic()))
        except asyncio.TimeoutError as exc:
            raise TurboError("DEADLINE_EXCEEDED") from exc
        except (OSError,ValueError) as exc:
            raise TurboError("WORKER_START_FAILED") from exc
        data = await self._rpc({"cmd": "LOAD", "model_dir": str(self.settings.model_dir)}, deadline)
        if data.get("model_revision") != MODEL_REVISION:
            raise TurboError("MODEL_IDENTITY_CHANGED")
        self.loads += int(data["loads"])
        self.device = data.get("device")
        if (not isinstance(self.device,dict) or not isinstance(self.device.get("name"),str)
            or not self.device["name"] or type(self.device.get("index")) is not int):
            raise TurboError("CUDA_PROOF_MISSING")
        self.state = "READY"
        # LOAD is the only production path that performs the full immutable
        # snapshot hash verification before importing the model.
        self.model_hash_verified = True

    async def invalidate(self, voice=None):
        if self.lock.locked():
            raise TurboError("WORKER_BUSY")
        if self.state == "READY" and self.proc:
            try:
                await self._rpc({"cmd": "INVALIDATE", "voice": voice},
                                time.monotonic() + 10)
            except BaseException as exc:
                await self._kill()
                raise TurboError("CACHE_INVALIDATION_WORKER_REAPED") from exc

    async def synthesize(self, *, profile: dict, reference: Path, text: str,
                         script: PauseScript | None, speed: float, seed: int | None) -> tuple[bytes, dict]:
        if self.lock.locked():
            raise TurboError("NATIVE_BUSY")
        async with self.lock:
            self.active_id = profile["id"]
            self._cancel_idle()
            deadline = time.monotonic() + self.settings.timeout
            started = time.monotonic()
            try:
                await self._ensure_loaded(deadline)
                parts = script.parts if script is not None else (SpeechPart(text),)
                speech_wavs = []
                chunk_hashes = []
                observed_model = observed_backend = observed_revision = None
                last_memory = {}
                max_segment_peak = None
                measured_segments = 0
                total_speech_generations = 0
                for part in parts:
                    if not isinstance(part, SpeechPart):
                        continue
                    try:
                        segments = segment_text(part.text, seed)
                    except SegmentationError as exc:
                        raise TurboError(exc.code) from exc
                    part_wavs = []
                    for segment in segments:
                        total_speech_generations += 1
                        chunk_hashes.append(segment.sha256)
                        self.state = "GENERATING"
                        answer = await self._rpc({
                            "cmd": "SYNTH", "voice": profile["id"], "version": profile["version"],
                            "reference": str(reference), "reference_sha": profile["ref_sha256"],
                            "text": segment.text, "seed": segment.seed,
                        }, deadline)
                        if (answer.get("model_id") != "chatterbox-nano"
                                or answer.get("backend") != "CUDA0"
                                or answer.get("model_revision") != MODEL_REVISION):
                            raise TurboError("MODEL_IDENTITY_CHANGED")
                        observed_model = answer["model_id"]
                        observed_backend = answer["backend"]
                        observed_revision = answer["model_revision"]
                        raw = base64.b64decode(answer["wav"], validate=True)
                        left=deadline-time.monotonic()
                        if left<=0: raise TurboError("DEADLINE_EXCEEDED")
                        try:
                            await asyncio.wait_for(asyncio.to_thread(
                                wav_info,raw,max_bytes=self.settings.max_output_bytes,
                                max_seconds=600),left)
                        except asyncio.TimeoutError as exc:
                            raise TurboError("DEADLINE_EXCEEDED") from exc
                        if speed != 1.0:
                            left=deadline-time.monotonic()
                            if left<=0: raise TurboError("DEADLINE_EXCEEDED")
                            try:
                                raw=await asyncio.wait_for(
                                    stretch_speech_async(raw,speed,left,self.settings.max_output_bytes,
                                                         self.settings.cache_dir/"tmp"),left)
                            except (asyncio.TimeoutError,AudioError) as exc:
                                if (isinstance(exc,asyncio.TimeoutError) or
                                    str(exc)=="ATEMPO_TIMEOUT" or time.monotonic()>=deadline):
                                    raise TurboError("DEADLINE_EXCEEDED") from exc
                                raise
                        part_wavs.append(raw)
                        if isinstance(answer.get("memory"), dict):
                            last_memory = answer["memory"].copy()
                            segment_peak=last_memory.get("peak_bytes")
                            if (last_memory.get("peak_scope")=="generation"
                                and type(segment_peak) is int and segment_peak>=0):
                                max_segment_peak=max(segment_peak,max_segment_peak or 0)
                                measured_segments+=1
                        self.hits = answer.get("cache_hits", self.hits)
                        self.syntheses = answer.get("syntheses", self.syntheses)
                    speech_wavs.append(part_wavs[0] if len(part_wavs) == 1 else
                                       await asyncio.to_thread(concatenate, part_wavs))
                left=deadline-time.monotonic()
                if left<=0: raise TurboError("DEADLINE_EXCEEDED")
                try:
                    data,meta,info=await asyncio.wait_for(asyncio.to_thread(
                        finalize_audio,script,speech_wavs,self.settings.max_output_bytes),left)
                except asyncio.TimeoutError as exc:
                    raise TurboError("DEADLINE_EXCEEDED") from exc
                if time.monotonic()>=deadline:
                    raise TurboError("DEADLINE_EXCEEDED")
                if max_segment_peak is not None and measured_segments == total_speech_generations:
                    last_memory["peak_bytes"]=max_segment_peak
                    last_memory["peak_scope"]=(
                        "generation" if measured_segments==1 else "max_generation_in_request")
                else:
                    # A partial sample must not masquerade as the request peak.
                    last_memory.pop("peak_bytes",None)
                    last_memory.pop("peak_scope",None)
                elapsed = time.monotonic() - started
                self.memory = last_memory or None
                speech_s = meta["speech_frames"] / 24000
                audio_s = info["seconds"]
                self.state = "READY"
                self.last_error = None
                self.live_generation_verified_at = time.time()
                return data, {**meta, "audio_s": audio_s, "speech_s": speech_s,
                              "elapsed_s": elapsed, "deadline_at":deadline,
                              "rtf": elapsed / audio_s,
                              "speech_rtf": elapsed / speech_s,
                              "device": self.device, "memory": last_memory,
                              "speed_mode": "postprocess_atempo",
                              "native_speech_segments": total_speech_generations,
                              "chunk_text_sha256": chunk_hashes,
                              "seed_policy_version": POLICY_VERSION,
                              "model": observed_model, "backend": observed_backend,
                              "model_revision": observed_revision}
            except BaseException as exc:
                self.last_error = exc.code if isinstance(exc, TurboError) else "GENERATION_FAILED"
                await self._kill()
                self.state = "ERROR"
                raise
            finally:
                self.active_id = None
                if self.state == "READY":
                    self._arm_idle()
