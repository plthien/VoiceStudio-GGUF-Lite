"""N04 and speed fallback regressions, not claims of owner GTX1650 proof."""
import asyncio
import hashlib
import json
import math
import wave
import io
from pathlib import Path
from fastapi.testclient import TestClient
import httpx
import pytest

from lite.app import create_app
from lite.config import Settings
from lite.speed import native_speed_capability,MARKER,digest

def settings(tmp):
    return Settings(data_dir=tmp/"data",cache_dir=tmp/"cache",bin_dir=tmp/"bin",timeout=30)

def make_wav():
    out=io.BytesIO()
    with wave.open(out,"wb") as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000);w.writeframes(b"\x01\x00"*6000)
    return out.getvalue()

def test_n04_cheap_busy_includes_mutation_admission_no_preflight(tmp_path,monkeypatch):
    s=settings(tmp_path)
    app=create_app(s)
    monkeypatch.setattr("lite.app.preflight",lambda *_: (_ for _ in ()).throw(AssertionError("Heavy preflight ran")))
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://127.0.0.1:3900") as c:
            before=(await c.get("/lite/status")).json()
            assert before["busy"] is False
            assert before["native_busy"] is False and before["operation_busy"] is False
            await app.state.admission.acquire()
            try:
                result=(await c.get("/lite/status")).json()
                assert result["busy"] is True and result["operation_busy"] is True
                assert result["native_busy"] is False
                assert (await c.post("/v1/audio/speech",json={"model":"omnivoice-gguf",
                    "voice":"sample","input":"hello","speed":1.0})).status_code==429
                assert (await c.delete("/profiles/sample")).status_code==409
                assert (await c.get("/health")).status_code==200
            finally:
                app.state.admission.release()
            assert (await c.get("/lite/status")).json()["busy"] is False
            await app.state.runner.lock.acquire()
            try:
                state=(await c.get("/lite/status")).json()
                assert state["busy"] is True and state["native_busy"] is True
            finally:
                app.state.runner.lock.release()
            assert (await c.get("/lite/status")).json()["busy"] is False
    asyncio.run(exercise())

@pytest.mark.parametrize("bad",[-1,0,0.7,1.26,2.0,float("nan"),float("inf"),float("-inf")])
def test_speed_invalid_rejected_before_native_admission(tmp_path,bad):
    s=settings(tmp_path)
    app=create_app(s)
    with TestClient(app) as c:
        payload={"model":"omnivoice-gguf","voice":"safe","input":"something","speed":bad}
        if not math.isfinite(bad):
            # JSON standard forbids nonfinite numeric literals; validate model
            # directly rather than expecting TestClient to emit invalid JSON.
            from pydantic import ValidationError
            from lite.app import Speech
            with pytest.raises(ValidationError):
                Speech.model_validate(payload)
        else:
            r=c.post("/v1/audio/speech",json=payload)
            assert r.status_code==422

def test_speed_nondefault_old_binary_fails_before_native_or_gpu(tmp_path,monkeypatch):
    s=settings(tmp_path);app=create_app(s)
    app.state.store.get=lambda id:{"id":"a","name":"A","ref_text":"words","language":"en"} if id=="a" else None
    app.state.licence.is_ready=lambda:True
    monkeypatch.setattr("lite.app.native_speed_capability",
                        lambda _settings:{"supported":False,"reason":"old runtime"})
    async def forbidden(**kw):
        raise AssertionError("Native spawn was attempted")
    app.state.runner.generate=forbidden
    with TestClient(app) as c:
        r=c.post("/v1/audio/speech",json={"model":"omnivoice-gguf",
            "voice":"a","input":"hello world","speed":1.2})
        assert r.status_code==503
        assert r.json()["error"]["code"]=="NATIVE_SPEED_UNAVAILABLE"
        assert not app.state.runner.lock.locked()
        assert not app.state.admission.locked()
        assert c.get("/lite/speed-capability").status_code==200
        # Old callers with speed omitted do not encounter a speed capability gate.
        # Existing native-related contract tests prove that successful default
        # generation still returns verified 24k WAV.
        r2=c.post("/v1/audio/speech",json={"model":"omnivoice-gguf",
            "voice":"missing","input":"hello"})
        assert r2.status_code==404

def test_speed_capability_receipt_missing_invalid_or_forged_marker(tmp_path,monkeypatch):
    from lite.config import ROOT,RUNTIME_REVISION,NATIVE_NAME,VULKAN_NAME
    s=settings(tmp_path);s.bin_dir.mkdir()
    exe=s.bin_dir/NATIVE_NAME;dll=s.bin_dir/VULKAN_NAME
    exe.write_bytes(b"MZ"+b"x"*200);dll.write_bytes(b"MZ"+b"y"*200)
    manifest=s.bin_dir/"checksums.sha256"
    manifest.write_text("".join(digest(p)+"  "+p.name+"\n" for p in (exe,dll)))
    assert native_speed_capability(s)["supported"] is False
    receipt=s.bin_dir/"speed-capability.json"
    valid={"schema":"lite-native-speed-v1","runtime_revision":RUNTIME_REVISION,
           "exe_sha256":digest(exe),
           "patch_sha256":digest(ROOT/"scripts/internal/instrument-native-speed.py"),
           "header_sha256":digest(ROOT/"scripts/internal/lite-native-speed.h")}
    receipt.write_text(json.dumps(valid))
    class FakeCompleted:
        returncode=0
        stdout=(MARKER+" --speed").encode()
        stderr=b""
    monkeypatch.setattr("lite.speed.subprocess.run",lambda *a,**kw:FakeCompleted())
    assert native_speed_capability(s)["supported"] is True
    receipt.write_text(json.dumps({**valid,"exe_sha256":"0"*64}))
    assert native_speed_capability(s)["supported"] is False
    receipt.write_text(json.dumps(valid))
    exe.write_bytes(b"MZ"+b"forged patch binary"*30)
    assert native_speed_capability(s)["supported"] is False
    exe.write_bytes(b"MZ"+b"x"*200)
    monkeypatch.setattr("lite.speed.subprocess.run",lambda *a,**kw:
                        type("C",(),{"returncode":0,"stdout":b"regular help","stderr":b""})())
    assert native_speed_capability(s)["supported"] is False

def test_speed_nondefault_api_passes_typed_runner_kwarg_and_headers(tmp_path,monkeypatch):
    s=settings(tmp_path);app=create_app(s)
    monkeypatch.setattr("lite.app.native_speed_capability",lambda _:{"supported":True})
    app.state.licence.is_ready=lambda:True
    app.state.store.get=lambda voice:{"id":"voice","name":"Voice","ref_text":"A","language":"en"}
    app.state.store.reference=lambda row:Path("/synthetic/ref.wav")
    observed=[]
    async def fake_generate(**kw):
        observed.append(kw)
        return make_wav(),{"request_id":kw["request_id"],"verification_status":"verified",
            "observed_native_backend":"Vulkan0","observed_device_identity":"NVIDIA GTX 1650",
            "audio_s":0.25,"elapsed_s":0.1,"rtf":0.4}
    app.state.runner.generate=fake_generate
    with TestClient(app) as c:
        first=c.post("/v1/audio/speech",json={"model":"omnivoice-gguf",
            "voice":"voice","input":"hello","speed":1.2})
        assert first.status_code==200,first.text
        assert first.headers["x-lite-requested-speed"]=="1.20"
        assert observed[-1]["speed"]==1.2
        baseline=c.post("/v1/audio/speech",json={"model":"omnivoice-gguf",
            "voice":"voice","input":"hello"})
        assert baseline.status_code==200
        assert "speed" not in observed[-1]
        assert baseline.headers["x-lite-requested-speed"]=="1.00"
