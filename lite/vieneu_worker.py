"""VieNeu-only CPU/ONNX subprocess. Never imported by the Chatterbox HTTP process."""
from __future__ import annotations
import base64
import hashlib
import io
import json
import os
import sys
import tempfile
import wave
from pathlib import Path

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["CUDA_VISIBLE_DEVICES"] = ""

from .vieneu_config import MODEL_REVISION, root_dir, model_dir, verify_model

model = None
PREFIX = "VIE1:"

def reply(obj):
    sys.stdout.write(PREFIX + json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()

def _within(path: str, base: Path, *, exists: bool = True) -> Path:
    base = base.resolve()
    p = Path(path).resolve(strict=exists)
    if not p.is_relative_to(base) or (exists and not p.is_file()) or p.is_symlink():
        raise ValueError("INVALID_JOB_PATH")
    return p

def _load():
    global model
    if model is None:
        verify_model()
        from vieneu import Vieneu
        model = Vieneu(mode="v3nano", onnx_dir=str(model_dir()),
                       threads=int(os.getenv("VIENEU_CPU_THREADS", "4")), steps=16, cfg=3.0)
        if model.sample_rate != 24000 or model.backend != "onnx":
            raise RuntimeError("INVALID_CPU_ONNX_PROVIDER")
    return model

def _pcm_wav(samples):
    import numpy as np
    arr = np.asarray(samples, dtype=np.float32).reshape(-1)
    if not arr.size or not np.all(np.isfinite(arr)):
        raise ValueError("INVALID_AUDIO")
    pcm = np.rint(np.clip(arr, -1, 1) * 32767).astype("<i2").tobytes()
    f = io.BytesIO()
    with wave.open(f, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
        w.writeframes(pcm)
    return f.getvalue()

def _atomic(path: Path, payload: bytes):
    temp = path.with_suffix(path.suffix + ".partial")
    try:
        with temp.open("xb") as w:
            w.write(payload); w.flush(); os.fsync(w.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)

def handle(message: dict) -> dict:
    global model
    action = message.get("action")
    if action == "ping":
        return {"state": "ready", "resident": model is not None, "model_revision": MODEL_REVISION}
    if action == "unload":
        if model is not None:
            model.close()
        model = None
        return {"unloaded": True}
    engine = _load()
    if action == "presets":
        return {"presets": [{"id": "preset:" + name, "name": label,
                             "voice_type": "preset", "provider_id": "vieneu-v3-nano"}
                            for label, name in engine.list_preset_voices()]}
    if action == "enroll":
        root = root_dir() / "cache" / "staging"
        src = _within(message["reference"], root)
        dst = _within(message["conditioning"], root, exists=False)
        spk, style = engine.encode_reference(str(src), denoise=False)
        obj = {"speaker_emb": spk.tolist(), "style": style.tolist()}
        payload = json.dumps(obj, separators=(",", ":"), allow_nan=False).encode("utf-8")
        _atomic(dst, payload)
        return {"conditioning_sha256": hashlib.sha256(payload).hexdigest(),
                "conditioning_bytes": len(payload)}
    if action == "synthesize":
        job_id = message["job_id"]
        if len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
            raise ValueError("INVALID_JOB_ID")
        folder = root_dir() / "cache" / "jobs" / job_id
        if folder.is_symlink() or not folder.is_dir():
            raise ValueError("INVALID_JOB_PATH")
        voice = message["voice"]
        if isinstance(voice, str) and voice.startswith("preset:"):
            voice = voice[len("preset:"):]
            if voice not in [v for _, v in engine.list_preset_voices()]:
                raise ValueError("PROFILE_NOT_FOUND")
        elif isinstance(voice, dict):
            import numpy as np
            voice = {"speaker_emb": np.asarray(voice["speaker_emb"], dtype=np.float32),
                     "style": np.asarray(voice["style"], dtype=np.float32)}
            if voice["speaker_emb"].shape != (192,) or voice["style"].shape != (50, 256):
                raise ValueError("PROFILE_INTEGRITY_FAILED")
        else:
            raise ValueError("PROFILE_NOT_FOUND")
        paths = []
        for i, text in enumerate(message["segments"]):
            samples = engine.infer(text, voice=voice,
                                   speed=message["speed"], steps=message["steps"],
                                   cfg=message["cfg"], sway=message["sway"],
                                   seed=None if message["seed"] is None else message["seed"] + i,
                                   max_chars=140, apply_watermark=False)
            payload = _pcm_wav(samples)
            target = folder / ("seg-%04d.wav" % i)
            _atomic(target, payload)
            paths.append({"file": target.name, "sha256": hashlib.sha256(payload).hexdigest(),
                          "bytes": len(payload)})
        return {"segments": paths, "completed": len(paths)}
    raise ValueError("INVALID_ACTION")

def main():
    for line in sys.stdin:
        try:
            message = json.loads(line)
            result = handle(message)
            reply({"ok": True, "result": result})
        except (Exception, BaseException) as exc:
            # No full tracebacks or local file paths in the IPC surface.
            code = str(exc) if str(exc) in (
                "INVALID_JOB_PATH", "INVALID_JOB_ID", "PROFILE_NOT_FOUND",
                "PROFILE_INTEGRITY_FAILED", "MODEL_MANIFEST_UNAVAILABLE",
                "MODEL_MANIFEST_INVALID", "MODEL_HASH_MISMATCH",
                "MODEL_FILES_MISSING", "INVALID_AUDIO", "INVALID_CPU_ONNX_PROVIDER",
                "INVALID_ACTION") else "VIENEU_WORKER_FAILED"
            reply({"ok": False, "code": code})
if __name__ == "__main__":
    main()
