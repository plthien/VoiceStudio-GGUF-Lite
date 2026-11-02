"""Post-UI V1 regression probes. Before repair these expose F01/F02/F03/F04."""
import asyncio
import io
import sqlite3
import wave
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from lite.app import create_app
from lite.config import Settings
from lite.ui_support import legacy_sources

def wav():
    f=io.BytesIO()
    with wave.open(f,"wb") as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000)
        w.writeframes(b"\\x01\\x00"*3000)
    return f.getvalue()

def setup(tmp,monkeypatch):
    s=Settings(data_dir=tmp/"data",cache_dir=tmp/"cache",bin_dir=tmp/"bin",timeout=30)
    monkeypatch.setattr("lite.profiles.shutil.which",lambda x:"ffmpeg")
    monkeypatch.setattr("lite.profiles.convert_reference",lambda src,dst,**k:dst.write_bytes(wav()))
    return create_app(s),s

def test_f01_real_omnivoice_default_db(tmp_path,monkeypatch):
    root=tmp_path/"OmniVoice";(root/"voices").mkdir(parents=True)
    with sqlite3.connect(root/"omnivoice.db") as con:
        con.execute("CREATE TABLE voice_profiles(id TEXT,name TEXT,ref_audio_path TEXT,ref_text TEXT,language TEXT,kind TEXT)")
    monkeypatch.setenv("APPDATA",str(tmp_path))
    monkeypatch.delenv("GGUF_LITE_LEGACY_ROOT",raising=False)
    monkeypatch.delenv("GGUF_LITE_LEGACY_DB",raising=False)
    monkeypatch.delenv("GGUF_LITE_LEGACY_VOICES",raising=False)
    db,refs=legacy_sources()
    assert db==root/"omnivoice.db" and refs==root/"voices"

def test_f02_ffmpeg_readiness_contract(tmp_path,monkeypatch):
    app,s=setup(tmp_path,monkeypatch)
    monkeypatch.setattr("lite.app.preflight",lambda settings:{"ready":False,"issues":[],"quant":{},"gpu":None,"binary":False})
    monkeypatch.setattr("lite.app.shutil.which",lambda x:"C:/ffmpeg" if x=="ffmpeg" else None)
    with TestClient(app) as c:
        assert c.get("/lite/readiness").json()["ffmpeg_installed"] is True
        assert c.get("/lite/status").json()["ffmpeg_installed"] is True
        monkeypatch.setattr("lite.app.shutil.which",lambda x:None)
        assert c.get("/lite/readiness").json()["ffmpeg_installed"] is False

def test_f03_reference_consent_is_version_specific(tmp_path,monkeypatch):
    app,s=setup(tmp_path,monkeypatch)
    with TestClient(app) as c:
        old=c.post("/profiles",data={"name":"Owned","ref_text":"spoken","consent":"owner-confirmed-local"},
          files={"ref_audio":("old.wav",wav(),"audio/wav")}).json()
        assert old["consent"]=="owner-confirmed-local"
        path="/profiles/"+old["id"]+"/audio"
        response=c.put(path,data={"consent":"owner-confirmed-local"},
          files={"ref_audio":("new.wav",wav(),"audio/wav")})
        assert response.status_code==200,response.text
        assert response.json()["version"]==2
        assert response.json()["consent"]=="owner-confirmed-local"
        legacy=c.put(path,files={"ref_audio":("third.wav",wav(),"audio/wav")})
        assert legacy.status_code==200
        assert legacy.json()["consent"] is None and legacy.json()["version"]==3

def test_f04_profile_mutation_cannot_overtake_unadmitted_generation(tmp_path,monkeypatch):
    app,s=setup(tmp_path,monkeypatch)
    row=app.state.store.import_audio(name="Voice",ref_text="actual",language="en",source=wav(),suffix=".wav")
    app.state.licence.is_ready=lambda:True
    entered=asyncio.Event()
    release=asyncio.Event()
    async def suspended_generate(**kwargs):
        entered.set()
        await release.wait()
        return wav(),{"request_id":"r","verification_status":"verified",
            "observed_native_backend":"Vulkan0",
            "observed_device_identity":"NVIDIA GTX 1650"}
    app.state.runner.generate=suspended_generate
    async def attempt():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://127.0.0.1:3900") as client:
            a=asyncio.create_task(client.post("/v1/audio/speech",
                json={"model":"omnivoice-gguf","voice":row["id"],"input":"Hello","speed":1.0}))
            await asyncio.wait_for(entered.wait(),timeout=3)
            b=await client.put("/profiles/"+row["id"],json={"ref_text":"different"})
            release.set()
            done=await asyncio.wait_for(a,timeout=5)
            assert b.status_code==409,"PROFILE_MUTATION_OVERRAN_SELECTED_INFERENCE"
            assert done.status_code==200
            assert app.state.store.get(row["id"])["ref_text"]=="actual"
    asyncio.run(attempt())

@pytest.mark.parametrize("mode",["replace","purge","migration","second-speech"])
def test_f04_adversarial_mutation_and_parallel_tts_blocked(tmp_path,monkeypatch,mode):
    app,s=setup(tmp_path,monkeypatch)
    row=app.state.store.import_audio(name="Voice",ref_text="known words",language="en",source=wav(),suffix=".wav")
    original=app.state.store.reference(row).read_bytes()
    app.state.licence.is_ready=lambda:True
    entered=asyncio.Event()
    release=asyncio.Event()
    async def generate(**kwargs):
        entered.set()
        await release.wait()
        return wav(),{"request_id":"r","verification_status":"verified",
            "observed_native_backend":"Vulkan0",
            "observed_device_identity":"NVIDIA GTX 1650"}
    app.state.runner.generate=generate
    def migration_should_not_run(*args,**kwargs):
        raise AssertionError("Migration entered before native completed")
    if mode=="migration":
        monkeypatch.setattr("lite.app.legacy_sources",lambda:(Path("db"),Path("refs")))
        monkeypatch.setattr("lite.app.migration_apply",migration_should_not_run)
    async def attempt():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://127.0.0.1:3900") as client:
            payload={"model":"omnivoice-gguf","voice":row["id"],"input":"Hello","speed":1.0}
            a=asyncio.create_task(client.post("/v1/audio/speech",json=payload))
            await asyncio.wait_for(entered.wait(),timeout=3)
            if mode=="replace":
                result=await client.put("/profiles/"+row["id"]+"/audio",
                    files={"ref_audio":("changed.wav",wav(),"audio/wav")})
            elif mode=="purge":
                result=await client.delete("/profiles/"+row["id"]+"?purge=true")
            elif mode=="migration":
                result=await client.post("/lite/migration/apply",json={"voice_id":row["id"]})
            else:
                result=await client.post("/v1/audio/speech",json=payload)
            release.set()
            response=await asyncio.wait_for(a,timeout=6)
            assert result.status_code==(429 if mode=="second-speech" else 409)
            assert response.status_code==200
            assert app.state.store.get(row["id"])["ref_text"]=="known words"
            assert app.state.store.reference(row).read_bytes()==original
    asyncio.run(attempt())

def test_f01_trusted_override_and_parent_escape(tmp_path,monkeypatch):
    root=tmp_path/"trusted";(root/"refs").mkdir(parents=True)
    db=root/"custom.db"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE voice_profiles(id TEXT,name TEXT,ref_audio_path TEXT,ref_text TEXT,language TEXT,kind TEXT)")
    monkeypatch.setenv("GGUF_LITE_LEGACY_ROOT",str(root))
    monkeypatch.setenv("GGUF_LITE_LEGACY_DB","custom.db")
    monkeypatch.setenv("GGUF_LITE_LEGACY_VOICES","refs")
    assert legacy_sources()==(db,root/"refs")
    monkeypatch.setenv("GGUF_LITE_LEGACY_DB","../custom.db")
    with pytest.raises(ValueError):
        legacy_sources()
    monkeypatch.setenv("GGUF_LITE_LEGACY_DB",str(tmp_path/"outside.db"))
    with pytest.raises((ValueError,FileNotFoundError)):
        legacy_sources()

def test_f03_invalid_replacement_rolls_back_consent_and_audio(tmp_path,monkeypatch):
    app,s=setup(tmp_path,monkeypatch)
    with TestClient(app) as c:
        old=c.post("/profiles",data={"name":"Owned","ref_text":"spoken","consent":"yes"},
            files={"ref_audio":("old.wav",wav(),"audio/wav")}).json()
        sid=old["id"]
        bad=c.put("/profiles/"+sid+"/audio",data={"consent":"new-confirmed"},
            files={"ref_audio":("bad.exe",b"x"*300,"application/octet-stream")})
        assert bad.status_code==422
        now=c.get("/profiles/"+sid).json()
        assert now["version"]==1 and now["consent"]=="yes"
        assert app.state.store.reference(now).read_bytes()==wav()
