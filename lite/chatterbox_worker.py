"""Isolated PyTorch worker. Only this process imports Chatterbox/CUDA."""
from __future__ import annotations

import base64
import contextlib
import copy
import gc
import io
import os
import sys
import wave
from pathlib import Path

from .chatterbox_config import ChatterboxSettings, MODEL_REVISION, verify_model_files
from .chatterbox_ipc import read_sync, write_sync, ProtocolError


def pcm_wav(audio) -> bytes:
    # Runtime model returns a tensor at 24kHz. Convert without remote code.
    # Reject non-finite model output *before* clamping; Inf must not be
    # silently translated into an apparently valid full-scale PCM sample.
    x = audio.detach().float().cpu().reshape(-1)
    import numpy as np
    raw_samples=x.numpy()
    if not np.isfinite(raw_samples).all():
        raise RuntimeError("NONFINITE_AUDIO")
    pcm = (np.clip(raw_samples,-1,1) * 32767).astype("<i2").tobytes()
    if not pcm:
        raise RuntimeError("EMPTY_AUDIO")
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(pcm)
    return out.getvalue()


class Engine:
    def __init__(self):
        self.model = None
        self.torch = None
        self.cache = {}
        self.active = None
        self.loads = 0
        self.syntheses = 0
        self.hits = 0
        self.device = None
        self.lifetime_peak_bytes = None

    def load(self, model_dir: str):
        if self.model is not None:
            return self.status()
        settings = ChatterboxSettings.load()
        folder = Path(model_dir).resolve()
        if folder != settings.model_dir.resolve():
            raise RuntimeError("MODEL_PATH_UNTRUSTED")
        verified, issues = verify_model_files(settings)
        if not verified:
            raise RuntimeError(issues[0])
        import torch
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA_UNAVAILABLE")
        try:
            index = torch.cuda.current_device()
            props = torch.cuda.get_device_properties(index)
            available, total = torch.cuda.mem_get_info(index)
            # Refuse when another resident model has consumed most of 4GB.
            if available < 1400 * 1024 * 1024:
                raise RuntimeError("CUDA_LOW_MEMORY_STOP_GGUF")
            with open(os.devnull, "w") as output, contextlib.redirect_stdout(output):
                from chatterbox.tts_turbo import ChatterboxTurboTTS
                model = ChatterboxTurboTTS.from_local(str(folder), device="cuda")
            if model is None:
                raise RuntimeError("MODEL_LOAD_FAILED")
            self.model = model
            self.torch = torch
            self.device = {"name": props.name, "index": index,
                           "total_bytes": total, "free_bytes_on_load": available}
            self.loads += 1
            return self.status()
        except torch.cuda.OutOfMemoryError as exc:
            raise RuntimeError("CUDA_OOM") from exc

    def status(self):
        return {"loaded": self.model is not None, "loads": self.loads,
                "syntheses": self.syntheses, "cache_hits": self.hits,
                "device": self.device, "model_revision": MODEL_REVISION}

    def generate(self, request):
        if self.model is None:
            raise RuntimeError("MODEL_UNLOADED")
        # No arbitrary reference path: service provides a store-contained path,
        # and worker re-checks its content hash against the snapshot.
        import hashlib
        ref = Path(request["reference"])
        root = ChatterboxSettings.load().data_dir / "voices"
        real = ref.resolve(strict=True)
        if not real.is_file() or not real.is_relative_to(root.resolve()):
            raise RuntimeError("REFERENCE_UNTRUSTED")
        if hashlib.sha256(real.read_bytes()).hexdigest() != request["reference_sha"]:
            raise RuntimeError("REFERENCE_CHANGED")
        with wave.open(str(real), "rb") as w:
            if w.getframerate() != 24000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
                raise RuntimeError("REFERENCE_INVALID")
            if w.getnframes() <= 5 * 24000:
                raise RuntimeError("REFERENCE_TOO_SHORT")
        key = (request["voice"], int(request["version"]), request["reference_sha"], MODEL_REVISION)
        try:
            if key in self.cache:
                self.hits += 1
                # Cache is immutable and CPU-owned; move a separate copy to GPU.
                self.model.conds = copy.deepcopy(self.cache[key]).to("cuda")
            else:
                with self.torch.inference_mode():
                    with open(os.devnull, "w") as output, contextlib.redirect_stdout(output):
                        self.model.prepare_conditionals(str(real))
                self.cache[key] = copy.deepcopy(self.model.conds).to("cpu")
                # Bounded cache; no competing voice conditionals pinned in 4GB VRAM.
                while len(self.cache) > 2:
                    self.cache.pop(next(iter(self.cache)))
                self.model.conds = copy.deepcopy(self.cache[key]).to("cuda")
            self.active = key
            seed = request.get("seed")
            if seed is not None:
                self.torch.manual_seed(int(seed))
                self.torch.cuda.manual_seed_all(int(seed))
            # Serial worker permits resetting peak at the exact synthesis boundary.
            reset_peak = getattr(self.torch.cuda, "reset_peak_memory_stats", None)
            measured_peak = callable(reset_peak)
            if measured_peak:
                # Capture pre-reset high water (load + reference conditioning)
                # before defining an isolated per-synthesis peak window.
                inherited_peak=int(self.torch.cuda.max_memory_allocated())
                self.lifetime_peak_bytes=max(inherited_peak,self.lifetime_peak_bytes or 0)
                reset_peak()
            with self.torch.inference_mode():
                with open(os.devnull, "w") as output, contextlib.redirect_stdout(output):
                    generated = self.model.generate(request["text"])
            self.syntheses += 1
            free_bytes, _ = self.torch.cuda.mem_get_info()
            memory = {
                "allocated_bytes": int(self.torch.cuda.memory_allocated()),
                "reserved_bytes": int(self.torch.cuda.memory_reserved()),
                "free_bytes": int(free_bytes)
            }
            if measured_peak:
                peak = int(self.torch.cuda.max_memory_allocated())
                self.lifetime_peak_bytes = max(peak, self.lifetime_peak_bytes or 0)
                memory.update(peak_bytes=peak, peak_scope="generation",
                              lifetime_peak_bytes=self.lifetime_peak_bytes)
            from .audio import wav_info
            wav = pcm_wav(generated)
            wav_info(wav, max_bytes=100 * 1024 * 1024, max_seconds=600)
            return {"wav": base64.b64encode(wav).decode("ascii"),
                    "device": self.device, "memory": memory, "cache_hits": self.hits,
                    "loads": self.loads, "syntheses": self.syntheses}
        except self.torch.cuda.OutOfMemoryError as exc:
            raise RuntimeError("CUDA_OOM") from exc

    def invalidate(self, voice=None):
        self.cache = {key: value for key, value in self.cache.items() if key[0] != voice} if voice else {}
        if voice is None or (self.active and self.active[0] == voice):
            self.active = None
            if self.model is not None:
                self.model.conds = None
        return self.status()


def run():
    engine = Engine()
    while True:
        try:
            message = read_sync()
            if message is None:
                break
            cmd = message.get("cmd")
            if cmd == "LOAD":
                answer = engine.load(message["model_dir"])
            elif cmd == "SYNTH":
                answer = engine.generate(message)
            elif cmd == "STATUS":
                answer = engine.status()
            elif cmd == "INVALIDATE":
                answer = engine.invalidate(message.get("voice"))
            elif cmd == "EXIT":
                write_sync({"ok": True})
                break
            else:
                raise RuntimeError("UNKNOWN_COMMAND")
            write_sync({"ok": True, "result": answer})
        except (KeyboardInterrupt, SystemExit):
            break
        except BaseException as exc:
            # No exception message, stack trace, path, reference text or voice ID.
            code = str(exc) if str(exc) in {
                "CUDA_OOM", "CUDA_UNAVAILABLE", "CUDA_LOW_MEMORY_STOP_GGUF", "MODEL_PATH_UNTRUSTED",
                "MODEL_HASH_MISMATCH", "MODEL_FILES_MISSING", "MODEL_REVISION_MISMATCH",
                "MODEL_MANIFEST_UNAVAILABLE", "REFERENCE_UNTRUSTED", "REFERENCE_CHANGED",
                "REFERENCE_INVALID", "REFERENCE_TOO_SHORT", "EMPTY_AUDIO",
                "MODEL_UNLOADED", "UNKNOWN_COMMAND", "MODEL_LOAD_FAILED", "NONFINITE_AUDIO"} else "WORKER_FAILURE"
            try:
                write_sync({"ok": False, "error": code})
            except BaseException:
                break


if __name__ == "__main__":
    run()
