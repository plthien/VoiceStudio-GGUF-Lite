"""Serial VieNeu worker owner; independent of Chatterbox GPU service and admissions."""
from __future__ import annotations
import asyncio
import base64
import contextlib
import hashlib
import json
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from .audio import wav_info
from .chatterbox_audio import concatenate
from .pause_audio import assemble_pause_wav
from .vieneu_config import (CONTRACT, MODEL_REVISION, SDK_REVISION, SDK_VERSION,
                            PROVIDER, VieNeuError, python_path, readiness, root_dir, verify_model)
from .vieneu_profiles import VoiceLibrary
from .vieneu_segmentation import build_job, POLICY

MAX_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_SECONDS = 600
MAX_UPLOAD_BYTES = 24 * 1024 * 1024

def _ram_status() -> dict:
    if os.name != 'nt':
        return {'ram_total_bytes': None, 'ram_available_bytes': None}
    try:
        import ctypes
        class MemoryStatus(ctypes.Structure):
            _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong),
                ('ullTotalPhys', ctypes.c_ulonglong), ('ullAvailPhys', ctypes.c_ulonglong),
                ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong),
                ('ullTotalVirtual', ctypes.c_ulonglong), ('ullAvailVirtual', ctypes.c_ulonglong),
                ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]
        mem = MemoryStatus(); mem.dwLength = ctypes.sizeof(mem)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):
            return {'ram_total_bytes': int(mem.ullTotalPhys), 'ram_available_bytes': int(mem.ullAvailPhys)}
    except (AttributeError, OSError):
        pass
    return {'ram_total_bytes': None, 'ram_available_bytes': None}


class VieNeuRuntime:
    def __init__(self, root: Path | None = None):
        self.root = (root or root_dir()).resolve()
        self.store = VoiceLibrary(self.root)
        self.lock = asyncio.Lock()
        self.process = None
        self.stderr_file = None
        self.last_error = None
        self.generation_count = 0
        self._preset_cache = None
        self._start_time = None

    def status(self) -> dict:
        p = self.process
        return {"provider": PROVIDER, "backend": "CPUExecutionProvider",
                "resident": p is not None and p.returncode is None,
                "busy": self.lock.locked(), "pid": p.pid if p and p.returncode is None else None,
                "cpu_threads": int(os.environ.get("VIENEU_CPU_THREADS", "4")),
                "generation_count": self.generation_count, "last_error": self.last_error,
                **_ram_status()}

    async def stop(self):
        p = self.process
        self.process = None
        self._preset_cache = None
        if p is not None and p.returncode is None:
            p.kill()
            try:
                await asyncio.wait_for(p.wait(), 10)
            except asyncio.TimeoutError:
                pass
        if self.stderr_file is not None:
            self.stderr_file.close()
            self.stderr_file = None

    async def unload(self):
        if self.lock.locked():
            raise VieNeuError("WORKER_BUSY")
        await self.stop()
        return {"unloaded": True}

    async def _start(self):
        if self.process and self.process.returncode is None:
            return
        await self.stop()
        state = readiness() if self.root == root_dir() else None
        if state is not None and not state["ready"]:
            raise VieNeuError(state["issues"][0])
        verify_model(self.root)
        py = python_path(self.root)
        if not py.is_file():
            raise VieNeuError("ENV_NOT_INSTALLED")
        (self.root / "logs").mkdir(parents=True, exist_ok=True)
        self.stderr_file = (self.root / "logs" / "vieneu-worker.log").open("ab")
        env = dict(os.environ)
        env["VIENEU_ROOT"] = str(self.root)
        env["HF_HUB_OFFLINE"] = "1"
        env["HF_HUB_DISABLE_TELEMETRY"] = "1"
        env["TRANSFORMERS_OFFLINE"] = "1"
        env["CUDA_VISIBLE_DEVICES"] = ""
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
        self.process = await asyncio.create_subprocess_exec(
            str(py), "-u", "-m", "lite.vieneu_worker",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=self.stderr_file, env=env, limit=8 * 1024 * 1024)
        self._start_time = time.monotonic()

    async def _rpc(self, action: dict, timeout: float):
        try:
            await self._start()
            p = self.process
            if p.stdin is None or p.stdout is None:
                raise VieNeuError("WORKER_CRASHED")
            p.stdin.write((json.dumps(action, ensure_ascii=False) + "\n").encode("utf-8"))
            await asyncio.wait_for(p.stdin.drain(), 10)
            async def receive():
                for _ in range(20):
                    line = await p.stdout.readline()
                    if not line:
                        raise VieNeuError("WORKER_CRASHED")
                    if line.startswith(b"VIE1:"):
                        return json.loads(line[5:])
                raise VieNeuError("WORKER_PROTOCOL_INVALID")
            response = await asyncio.wait_for(receive(), timeout)
            if not response.get("ok"):
                raise VieNeuError(response.get("code", "VIENEU_WORKER_FAILED"))
            return response["result"]
        except asyncio.TimeoutError as exc:
            self.last_error = "WORKER_TIMEOUT"
            await self.stop()
            raise VieNeuError("WORKER_TIMEOUT") from exc
        except asyncio.CancelledError:
            await self.stop()
            raise
        except (BrokenPipeError, EOFError, OSError, ValueError, json.JSONDecodeError) as exc:
            self.last_error = "WORKER_CRASHED"
            await self.stop()
            raise VieNeuError("WORKER_CRASHED") from exc
        except VieNeuError as exc:
            self.last_error = exc.code
            if exc.code in ("WORKER_CRASHED", "VIENEU_WORKER_FAILED", "WORKER_PROTOCOL_INVALID"):
                await self.stop()
            raise

    async def presets(self) -> list[dict]:
        async with self.lock:
            if self._preset_cache is None:
                answer = await self._rpc({"action": "presets"}, 120)
                self._preset_cache = answer["presets"]
            return self._preset_cache

    async def enroll(self, reference: bytes, *, name: str, voice_id: str | None = None) -> dict:
        async with self.lock:
            staging = self.root / "cache" / "staging"
            staging.mkdir(parents=True, exist_ok=True)
            job = uuid.uuid4().hex
            src, cond = staging / (job + ".wav"), staging / (job + ".json")
            try:
                src.write_bytes(reference)
                result = await self._rpc({"action": "enroll", "reference": str(src),
                                          "conditioning": str(cond)}, 180)
                data = cond.read_bytes()
                if hashlib.sha256(data).hexdigest() != result["conditioning_sha256"]:
                    raise VieNeuError("PROFILE_INTEGRITY_FAILED")
                return self.store.save(name, reference, data, voice_id)
            finally:
                src.unlink(missing_ok=True); cond.unlink(missing_ok=True)

    def _voice(self, voice_id: str) -> tuple[dict | str, dict]:
        if voice_id.startswith("preset:"):
            if self._preset_cache is None or voice_id not in [p["id"] for p in self._preset_cache]:
                raise VieNeuError("PROFILE_NOT_FOUND")
            return voice_id, {"voice_id": voice_id, "voice_version": None,
                              "conditioning_sha256": None, "reference_sha256": None}
        verified = self.store.verify(voice_id)
        data = json.loads(verified["conditioning"].read_text(encoding="utf-8"))
        return data, verified["profile"]

    async def synthesize(self, *, text: str, voice_id: str, speed: float = 1.0,
                         steps: int = 16, cfg: float = 3.0, sway: float = 0.0,
                         seed: int | None = None, timeout: float = 3600) -> tuple[bytes, dict]:
        job = build_job(text)
        async with self.lock:
            if voice_id.startswith("preset:") and self._preset_cache is None:
                self._preset_cache = (await self._rpc({"action": "presets"}, 120))["presets"]
            voice, identity = self._voice(voice_id)
            id_ = uuid.uuid4().hex
            folder = self.root / "cache" / "jobs" / id_
            folder.mkdir(parents=True)
            started = time.monotonic()
            try:
                result = await self._rpc(
                    {"action": "synthesize", "job_id": id_, "voice": voice,
                     "segments": list(job.spoken), "speed": speed, "steps": steps,
                     "cfg": cfg, "sway": sway, "seed": seed}, timeout)
                if result.get("completed") != len(job.spoken) or len(result.get("segments", [])) != len(job.spoken):
                    raise VieNeuError("AUDIO_INTEGRITY_FAILURE")
                wavs = []
                for i, info in enumerate(result["segments"]):
                    if info["file"] != "seg-%04d.wav" % i:
                        raise VieNeuError("AUDIO_INTEGRITY_FAILURE")
                    file = folder / info["file"]
                    if file.is_symlink() or not file.is_file() or file.resolve().parent != folder.resolve():
                        raise VieNeuError("AUDIO_INTEGRITY_FAILURE")
                    data = file.read_bytes()
                    if len(data) != info["bytes"] or hashlib.sha256(data).hexdigest() != info["sha256"]:
                        raise VieNeuError("AUDIO_INTEGRITY_FAILURE")
                    wav_info(data, max_bytes=MAX_OUTPUT_BYTES, max_seconds=120)
                    wavs.append(data)
                output, metrics = assemble_pause_wav(job.script, wavs,
                                      max_bytes=MAX_OUTPUT_BYTES, max_seconds=MAX_SECONDS)
                latest_identity = self._voice(voice_id)[1]
                if latest_identity != identity:
                    raise VieNeuError("REFERENCE_CHANGED")
                elapsed = time.monotonic() - started
                audio = wav_info(output, max_bytes=MAX_OUTPUT_BYTES, max_seconds=MAX_SECONDS)
                receipt = {"contract": CONTRACT, "model": PROVIDER,
                    "model_revision": MODEL_REVISION, "sdk_version": SDK_VERSION,
                    "sdk_revision": SDK_REVISION, "backend": "CPUExecutionProvider",
                    "input_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "voice_id": voice_id, "voice_version": identity.get("voice_version"),
                    "reference_sha256": identity.get("reference_sha256"),
                    "conditioning_sha256": identity.get("conditioning_sha256"),
                    "segmentation_policy": POLICY, "segments": len(wavs),
                    "steps": steps, "cfg": cfg, "sway": sway, "speed": speed, "seed": seed,
                    "output_sha256": audio["sha256"], "audio_frames": audio["frames"],
                    "audio_seconds": audio["seconds"], "elapsed_seconds": elapsed,
                    "pause_frames": job.script.pause_frames}
                self.generation_count += 1
                return output, {"receipt": receipt, "audio": audio, "metrics": metrics}
            finally:
                shutil.rmtree(folder, ignore_errors=True)
