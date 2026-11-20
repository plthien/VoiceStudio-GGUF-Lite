"""Pinned local-only VieNeu v3 Nano CPU model. Never consult HF during inference."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path

PROVIDER = "vieneu-v3-nano"
SDK_VERSION = "3.8.5"
SDK_REVISION = "6566468cf14974807050495036ab75cd041143f7"
MODEL_REPO = "pnnbao-ump/VieNeu-TTS-v3-Nano"
MODEL_REVISION = "aba295eb96a6fa6003ebe417cc1f2802a7adc1dc"
SAMPLE_RATE = 24000
CONTRACT = "vieneu_cpu_onnx_api_v1"
# SHA256 values are the actual Hugging Face file SHA256s, NOT Xet storage IDs.
MODEL_HASHES = {
    "text_encoder.onnx": "204f02cccae1f16ccb2d3840f05721a37fe250b82cbd456337a0ccb49615e4bf",
    "duration_predictor.onnx": "20fd7fa60006d0a48ee82e0451b3f920d2052588c083c756cce669f3947c0a68",
    "vector_estimator.onnx": "c6c1d4398ca35d3ad1bd3f0459413d1b975d09e7f2f3a2b4493a7b92bbf6ce93",
    "codec_decoder.onnx": "b0ab15e7828a39d53679e25b1ba4ba415a61311307202a6323130b9e1cc3029d",
    "constants.npz": "7c011938effe41687a9af85107a31a040dfcf0fb3d0f048ec10f4fb65c1a8829",
    "speaker_encoder.onnx": "a6ac6a63997761ae2997373e2ee1c47040854b4b759ea41ec48e4e42df0f4d73",
    "codec_encoder.onnx": "195418031f190bf1c72dbfbed8490574754b346769180209a423a96c45caab98",
    "reference_encoder.onnx": "6746906bfe857969a2b1b6e9091f1d4a19d9f04af4e187844021272dad4dcd3a",
}
# Config is pinned by immutable HF revision and SHA recorded at install time.
EXTRA_FILES = ("config.json",)
DEFAULT_ROOT = Path(r"D:\VoiceStudioBuild\vieneu-v3nano")

class VieNeuError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

def root_dir() -> Path:
    return Path(os.environ.get("VIENEU_ROOT", str(DEFAULT_ROOT))).resolve()

def python_path(root: Path | None = None) -> Path:
    r = root or root_dir()
    return r / ".venv-vieneu" / "Scripts" / "python.exe" if os.name == "nt" else r / ".venv-vieneu" / "bin" / "python"

def model_dir(root: Path | None = None) -> Path:
    return (root or root_dir()) / "models" / MODEL_REVISION

def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fd:
        for chunk in iter(lambda: fd.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def verify_model(root: Path | None = None) -> dict:
    base = model_dir(root)
    manifest = base / "manifest.json"
    if not manifest.is_file():
        raise VieNeuError("MODEL_MANIFEST_UNAVAILABLE")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if (data["model_repo"] != MODEL_REPO or data["model_revision"] != MODEL_REVISION
            or data["sdk_revision"] != SDK_REVISION or data["sdk_version"] != SDK_VERSION):
            raise VieNeuError("MODEL_MANIFEST_INVALID")
        expected = dict(MODEL_HASHES)
        config_sha = data["files"]["config.json"]
        if not isinstance(config_sha, str) or len(config_sha) != 64:
            raise VieNeuError("MODEL_MANIFEST_INVALID")
        expected["config.json"] = config_sha
        if any(data["files"].get(k) != v for k, v in expected.items()):
            raise VieNeuError("MODEL_MANIFEST_INVALID")
        for name, sha in expected.items():
            p = base / name
            if not p.is_file():
                raise VieNeuError("MODEL_FILES_MISSING")
            if digest(p) != sha:
                raise VieNeuError("MODEL_HASH_MISMATCH")
        return {"model_revision": MODEL_REVISION, "verified_files": len(expected)}
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise VieNeuError("MODEL_MANIFEST_INVALID") from exc

def readiness() -> dict:
    root = root_dir()
    issues = []
    if not python_path(root).is_file():
        issues.append("ENV_NOT_INSTALLED")
    try:
        verify_model(root)
    except VieNeuError as exc:
        issues.append(exc.code)
    return {"provider": PROVIDER, "backend": "CPUExecutionProvider", "ready": not issues,
            "worker_ready": not issues, "model_revision": MODEL_REVISION,
            "sdk_version": SDK_VERSION, "sdk_revision": SDK_REVISION, "issues": issues}
