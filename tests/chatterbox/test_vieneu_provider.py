"""Offline deterministic VieNeu policy, storage and API checks (no weights/GPU)."""
from __future__ import annotations
import asyncio
import hashlib
import io
import json
import threading
import time
import wave
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lite import vieneu_config as config
from lite.pause_audio import assemble_pause_wav
from lite.vieneu_profiles import VoiceLibrary
from lite.vieneu_segmentation import build_job, split_speech, POLICY
from lite.vieneu_config import VieNeuError
from lite import vieneu_routes

def sample_wav(seconds=1):
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
        w.writeframes(b"\x00\x00" * (24000 * seconds))
    return out.getvalue()

def conditioning():
    return json.dumps({"speaker_emb": [0.0] * 192,
                       "style": [[0.0] * 256 for _ in range(50)]}).encode()

def test_cloned_voice_is_provider_owned_versioned_and_survives_restart(tmp_path):
    store = VoiceLibrary(tmp_path)
    assert not (tmp_path / "data").exists()  # optional VieNeu must not block Chatterbox startup
    assert store.list() == []
    v1 = store.save("Giọng thử", sample_wav(6), conditioning())
    assert v1["provider_id"] == config.PROVIDER and v1["language"] == "vi"
    assert v1["voice_version"] == 1 and v1["consent"] == "confirmed"
    fresh = VoiceLibrary(tmp_path)
    assert fresh.verify(v1["voice_id"])["profile"]["reference_sha256"] == v1["reference_sha256"]
    renamed = fresh.rename(v1["voice_id"], "Giọng mới")
    assert renamed["voice_version"] == 1
    v2 = fresh.save("Giọng mới", sample_wav(7), conditioning(), v1["voice_id"])
    assert v2["voice_version"] == 2
    assert fresh.verify(v1["voice_id"])["profile"]["reference_sha256"] == v2["reference_sha256"]
    assert v2["reference_sha256"] != v1["reference_sha256"]
    assert len(fresh.list()) == 1
    fresh.delete(v1["voice_id"])
    assert fresh.list() == []

def test_corrupted_and_symlinked_reference_are_rejected(tmp_path):
    store = VoiceLibrary(tmp_path)
    item = store.save("Chuẩn", sample_wav(5), conditioning())
    v = store.verify(item["voice_id"])
    v["reference"].write_bytes(b"tampered")
    with pytest.raises(VieNeuError, match="PROFILE_INTEGRITY_FAILED"):
        store.verify(item["voice_id"])
    with pytest.raises(VieNeuError, match="PROFILE_NOT_FOUND"):
        store.get("../outside")

def test_vietnamese_segmentation_diacritics_and_pauses():
    input_text = ("Xin chào. Hôm nay chúng ta nói về tiếng Việt! " * 27 +
                  "[pause 800ms] Tiếp theo là lời kể. [pause 1.5s] Kết thúc.")
    job = build_job(input_text)
    assert job.script.pause_count == 2
    assert job.script.pause_frames == (800 + 1500) * 24
    assert job.script.speech_segments == len(job.spoken)
    assert len(job.spoken) > 2
    assert all(len(p) <= 700 and p for p in job.spoken)
    assert "tiếng Việt" in " ".join(job.spoken)
    sample = sample_wav()
    output, info = assemble_pause_wav(job.script, [sample for _ in job.spoken])
    assert info["audio_frames"] == len(job.spoken) * 24000 + job.script.pause_frames
    assert output.startswith(b"RIFF")
    assert POLICY.startswith("vi_request")
    with pytest.raises(ValueError):
        split_speech("hi" * 12001)

def test_pinned_snapshot_manifest_refuses_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MODEL_HASHES", {"test.onnx": hashlib.sha256(b"fixed").hexdigest()})
    base = config.model_dir(tmp_path); base.mkdir(parents=True)
    (base / "test.onnx").write_bytes(b"fixed")
    (base / "config.json").write_bytes(b'{"ok":true}')
    meta = {"model_repo": config.MODEL_REPO, "model_revision": config.MODEL_REVISION,
            "sdk_version": config.SDK_VERSION, "sdk_revision": config.SDK_REVISION,
            "files": {"test.onnx": hashlib.sha256(b"fixed").hexdigest(),
                      "config.json": config.digest(base / "config.json")}}
    (base / "manifest.json").write_text(json.dumps(meta))
    assert config.verify_model(tmp_path)["verified_files"] == 2
    (base / "test.onnx").write_bytes(b"malicious")
    with pytest.raises(VieNeuError, match="MODEL_HASH_MISMATCH"):
        config.verify_model(tmp_path)

class FakeVieNeuRuntime:
    def __init__(self, root):
        self.root = root
        self.store = VoiceLibrary(root)
        self.lock = asyncio.Lock()
        self.stopped = False
        self.calls = 0
        self.cancellations = 0
    def status(self):
        return {"provider": config.PROVIDER, "backend": "CPUExecutionProvider",
                "resident": True, "busy": self.lock.locked(), "cpu_threads": 4}
    async def stop(self):
        self.stopped = True
    async def unload(self):
        return {"unloaded": True}
    async def presets(self):
        return [{"id": "preset:Minh", "name": "Minh", "voice_type": "preset",
                 "provider_id": config.PROVIDER}]
    async def synthesize(self, *, text, voice_id, **kwargs):
        async with self.lock:
            self.calls += 1
            job = build_job(text)
            blob, frames = assemble_pause_wav(job.script, [sample_wav() for _ in job.spoken])
            return blob, {"receipt": {"audio_seconds": frames["audio_frames"] / 24000,
                "audio_frames": frames["audio_frames"], "output_sha256": hashlib.sha256(blob).hexdigest(),
                "voice_id": voice_id, "model_revision": config.MODEL_REVISION,
                "pause_frames": job.script.pause_frames}}

def test_router_contract_cpu_receipts_and_vietnamese_text(tmp_path, monkeypatch):
    fake = FakeVieNeuRuntime(tmp_path)
    monkeypatch.setattr(vieneu_routes, "VieNeuRuntime", lambda: fake)
    app = FastAPI()
    vieneu_routes.mount_vieneu(app)
    with TestClient(app) as client:
        paths = client.get("/openapi.json").json()["paths"]
        assert {"/vieneu/presets", "/vieneu/profiles", "/vieneu/v1/audio/speech",
                "/vieneu/worker/cancel", "/vieneu/worker/unload", "/vieneu/capability"} <= set(paths)
        assert client.get("/vieneu/capability").json()["backend"] == "CPUExecutionProvider"
        assert client.get("/vieneu/presets").json()[0]["id"] == "preset:Minh"
        data = {"model": config.PROVIDER, "language": "vi", "voice": "preset:Minh",
                "input": "Xin chào bạn. [pause 800ms] Tôi yêu tiếng Việt."}
        response = client.post("/vieneu/v1/audio/speech", json=data)
        assert response.status_code == 200, response.text
        assert response.headers["x-tts-backend"] == "CPUExecutionProvider"
        assert "x-tts-execution-receipt" not in response.headers  # Chatterbox contract is not reused
        assert response.headers["x-vieneu-contract"] == config.CONTRACT
        assert response.headers["x-vieneu-receipt"]
        assert response.content.startswith(b"RIFF") and fake.calls == 1
        invalid = client.post("/vieneu/v1/audio/speech", json={**data, "language": "en"})
        assert invalid.status_code == 422 and fake.calls == 1
        assert client.post("/vieneu/worker/unload").json()["unloaded"] is True

def test_windows_installer_and_single_ui_have_isolated_contracts():
    root = Path(__file__).resolve().parents[2]
    install = (root / "scripts/install-vieneu-v3nano.ps1").read_text()
    assert ".venv-vieneu" in install and "check-sdk" in install
    assert "download" in install and "verify" in install
    assert "torch==2" not in (root / "requirements-vieneu.txt").read_text()
    html = (root / "lite/chatterbox_ui/index.html").read_text()
    js = (root / "lite/chatterbox_ui/vieneu.js").read_text()
    assert 'id="provider-select"' in html
    assert 'id="vi-tts-form"' in html
    assert "/vieneu/v1/audio/speech" in js
    assert "/vieneu/worker/cancel" in js
    assert "/v1/audio/speech" in (root / "lite/chatterbox_app.py").read_text()
