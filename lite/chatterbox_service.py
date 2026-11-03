"""One serial, deadline-bounded persistent worker. Never replay uncertain generation."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import sys
import time
from pathlib import Path

from .audio import AudioError, wav_info
from .chatterbox_audio import stretch_speech_async
from .chatterbox_config import ChatterboxSettings, MODEL_REVISION, IDLE_CHOICES
from .chatterbox_ipc import send, recv
from .pause import PauseScript, SpeechPart, PausePart
from .pause_audio import assemble_pause_wav


class TurboError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


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

    def status(self):
        return {"state": self.state, "resident": bool(self.proc and self.proc.returncode is None),
                "pid": self.proc.pid if self.proc and self.proc.returncode is None else None,
                "busy": self.lock.locked(), "active": self.active_id is not None,
                "load_count": self.loads, "generation_count": self.syntheses,
                "condition_cache_hits": self.hits, "last_error": self.last_error,
                "cuda": self.device if self.device is not None else "OWNER TEST PENDING",
                "idle_remaining_seconds": max(0, self.idle_deadline - time.monotonic())
                    if self.idle_deadline is not None else None,
                "model_revision": MODEL_REVISION}

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
        if proc and proc.returncode is None:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                pass
        self.device = None
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
        except (OSError, EOFError, asyncio.IncompleteReadError, BrokenPipeError) as exc:
            raise TurboError("WORKER_CRASH") from exc
        if not result.get("ok"):
            raise TurboError(result.get("error", "WORKER_FAILURE"))
        return result.get("result", {})

    async def _ensure_loaded(self, deadline):
        self._cancel_idle()
        if self.proc and self.proc.returncode is None and self.state == "READY":
            return
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
        self.proc = await asyncio.wait_for(asyncio.create_subprocess_exec(
            str(python), "-m", "lite.chatterbox_worker", stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            cwd=str(root), env=env), max(0.001, deadline - time.monotonic()))
        data = await self._rpc({"cmd": "LOAD", "model_dir": str(self.settings.model_dir)}, deadline)
        self.loads += int(data["loads"])
        self.device = data.get("device")
        if not isinstance(self.device, dict) or not self.device.get("name"):
            raise TurboError("CUDA_PROOF_MISSING")
        self.state = "READY"

    async def invalidate(self, voice=None):
        if self.lock.locked():
            raise TurboError("WORKER_BUSY")
        if self.state == "READY" and self.proc:
            try:
                await self._rpc({"cmd": "INVALIDATE", "voice": voice},
                                time.monotonic() + 10)
            except BaseException:
                await self._kill()

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
                for i, part in enumerate(parts):
                    if not isinstance(part, SpeechPart):
                        continue
                    self.state = "GENERATING"
                    answer = await self._rpc({
                        "cmd": "SYNTH", "voice": profile["id"], "version": profile["version"],
                        "reference": str(reference), "reference_sha": profile["ref_sha256"],
                        "text": part.text, "seed": (seed + i) % 2147483648 if seed is not None else None,
                    }, deadline)
                    raw = base64.b64decode(answer["wav"], validate=True)
                    wav_info(raw, max_bytes=self.settings.max_output_bytes, max_seconds=600)
                    if speed != 1.0:
                        raw = await asyncio.wait_for(
                            stretch_speech_async(raw, speed,
                                deadline - time.monotonic(), self.settings.max_output_bytes,
                                self.settings.cache_dir / "tmp"),
                            max(0.001, deadline - time.monotonic()))
                    speech_wavs.append(raw)
                    self.hits = answer.get("cache_hits", self.hits)
                    self.syntheses = answer.get("syntheses", self.syntheses)
                if script:
                    data, meta = assemble_pause_wav(script, speech_wavs,
                        max_bytes=self.settings.max_output_bytes, max_seconds=600)
                else:
                    data = speech_wavs[0]
                    info = wav_info(data, max_bytes=self.settings.max_output_bytes, max_seconds=600)
                    meta = {"pause_count": 0, "pause_frames": 0, "speech_frames": info["frames"],
                            "audio_frames": info["frames"], "speech_segments": 1}
                info = wav_info(data, max_bytes=self.settings.max_output_bytes, max_seconds=600)
                if info["frames"] != meta["audio_frames"]:
                    raise TurboError("AUDIO_INTEGRITY_FAILURE")
                elapsed = time.monotonic() - started
                speech_s = meta["speech_frames"] / 24000
                audio_s = info["seconds"]
                self.state = "READY"
                self.last_error = None
                return data, {**meta, "audio_s": audio_s, "speech_s": speech_s,
                              "elapsed_s": elapsed, "rtf": elapsed / audio_s,
                              "speech_rtf": elapsed / speech_s,
                              "device": self.device, "speed_mode": "postprocess_atempo",
                              "model_revision": MODEL_REVISION}
            except BaseException as exc:
                self.last_error = exc.code if isinstance(exc, TurboError) else "GENERATION_FAILED"
                await self._kill()
                self.state = "ERROR"
                raise
            finally:
                self.active_id = None
                if self.state == "READY":
                    self._arm_idle()
