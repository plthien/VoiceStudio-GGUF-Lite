"""Pinned files, fake GPU and native error contracts."""
import asyncio
import hashlib
import io
import wave
from pathlib import Path
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
from lite.config import Settings, MODEL_REVISION, REPO_ID, BASE_NAME, TOKENIZER_NAME
from lite.runtime import (parse_vulkan_devices, native_files, model_pair, preflight,
                          RuntimeErrorLite)
from lite.native_runner import Runner, NativeBusy, NativeTimeout, NativeFailure
from lite.app import create_app

@pytest.fixture
def config(tmp_path):
    binary=tmp_path/"bin";binary.mkdir()
    cache=tmp_path/"models";cache.mkdir()
    data=tmp_path/"data";data.mkdir()
    return Settings(data_dir=data,cache_dir=cache,bin_dir=binary,timeout=30)

def test_model_pair_must_be_exact_q8(config):
    with pytest.raises(RuntimeErrorLite):
        model_pair(config)
    path=config.cache_dir/"models--Serveurperso--OmniVoice-GGUF"/"snapshots"/MODEL_REVISION
    path.mkdir(parents=True)
    (path/BASE_NAME).write_bytes(b"base")
    (path/TOKENIZER_NAME).write_bytes(b"token")
    with pytest.raises(RuntimeErrorLite):
        model_pair(config)  # fake file names cannot pass trusted Q8 digest
    (path/TOKENIZER_NAME).unlink()
    with pytest.raises(RuntimeErrorLite):
        model_pair(config)

def test_untrusted_native_manifest(config):
    a=config.bin_dir/"omnivoice-tts-windows-x86_64.exe"
    d=config.bin_dir/"ggml-vulkan.dll"
    a.write_bytes(b"MZ"+b"x"*200)
    d.write_bytes(b"MZ"+b"x"*200)
    with pytest.raises(RuntimeErrorLite):
        native_files(config)
    manifest=config.bin_dir/"checksums.sha256"
    manifest.write_text("\n".join([
        hashlib.sha256(a.read_bytes()).hexdigest()+"  "+a.name,
        hashlib.sha256(d.read_bytes()).hexdigest()+"  "+d.name])+"\n")
    assert native_files(config)==(a,d)
    d.write_bytes(b"MZ"+b"other"*100)
    with pytest.raises(RuntimeErrorLite):
        native_files(config)

def test_extra_ggml_library_must_be_hash_pinned(config):
    a=config.bin_dir/"omnivoice-tts-windows-x86_64.exe"
    d=config.bin_dir/"ggml-vulkan.dll"
    other=config.bin_dir/"ggml-core.dll"
    for f in (a,d,other):
        f.write_bytes(b"MZ"+b"x"*200)
    m=config.bin_dir/"checksums.sha256"
    m.write_text("\n".join(hashlib.sha256(f.read_bytes()).hexdigest()+"  "+f.name
                           for f in (a,d))+"\n")
    with pytest.raises(RuntimeErrorLite):
        native_files(config)

def test_preflight_does_not_claim_in_use_on_error(config,monkeypatch):
    monkeypatch.setattr("lite.runtime.nvidia_device",lambda: {"verified":True,"backend":"Vulkan0"})
    result=preflight(config)
    assert not result["ready"] and result["quant"]["in_use"] is None

def test_unambiguous_vulkan_enumeration():
    devices=parse_vulkan_devices("""GPU0:
deviceName = NVIDIA GeForce GTX 1650
deviceType = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
vendorID = 0x10de
""")
    assert len(devices)==1
    assert devices[0]["vk_index"]==0 and devices[0]["index"]==0

def test_native_busy_and_lifecycle_finite(config):
    runner=Runner(config)
    async def run():
        await runner.lock.acquire()
        try:
            with pytest.raises(NativeBusy):
                await runner.generate(profile={"id":"voice"},reference=Path("not-used"),
                    text="hello",language="en",seed=42,request_id="request")
        finally:
            runner.lock.release()
    asyncio.run(run())

def test_api_contract_wav_bytes_without_alias(config):
    app=create_app(config)
    row=app.state.store
    import lite.app as appmodule
    # Synthetic native fixture, not a claim of a working GPU.
    from lite.licence import Licence
    app.state.licence.is_ready=lambda:True
    row.get=lambda x: {"id":x,"ref_text":"hello","language":"en"} if x=="voice-001" else None
    row.reference=lambda r:Path("/fake/owned/reference.wav")
    sample=io.BytesIO()
    with wave.open(sample,"wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
        w.writeframes(b"\0\0"*100)
    async def fake_generate(**kwargs):
        return sample.getvalue(),{"request_id":"request-1",
                "verification_status":"verified","observed_native_backend":"Vulkan0",
                "observed_device_identity":"NVIDIA GeForce GTX 1650"}
    app.state.runner.generate=fake_generate
    with TestClient(app) as c:
        resp=c.post("/v1/audio/speech",json={"model":"omnivoice-gguf",
                         "voice":"voice-001","input":"hello","speed":1.0})
        assert resp.status_code==200 and resp.headers["content-type"]=="audio/wav"
        assert resp.content==sample.getvalue()
        assert c.post("/v1/audio/speech",json={"model":"omnivoice-gguf",
                     "voice":"alloy","input":"hello"}).status_code==404

def test_native_proofless_output_fails_closed_and_does_not_publish(config):
    app=create_app(config)
    app.state.licence.is_ready=lambda:True
    app.state.store.get=lambda x:{"id":"safe","ref_text":"words","language":"en"} if x=="safe" else None
    app.state.store.reference=lambda x:Path("/fake.wav")
    async def proofless(**kwargs):
        return b"RIFF",{"request_id":"r"}
    app.state.runner.generate=proofless
    with TestClient(app) as c:
        response=c.post("/v1/audio/speech",json={"model":"omnivoice-gguf",
            "voice":"safe","input":"Say this","speed":1.0})
        assert response.status_code==500
        assert response.json()["error"]["code"]=="NATIVE_PROOF_MISSING"
        assert response.headers["content-type"].startswith("application/json")
