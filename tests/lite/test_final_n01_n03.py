"""N01/N02/N03 final re-audit regressions. Preserve pre-fix red evidence."""
import io
import json
import wave
from fastapi.testclient import TestClient
from lite.app import create_app
from lite.config import Settings
from lite import preflight as cli

def wav_b():
    b=bytearray(wav())
    b[-2]=7
    return bytes(b)

def wav():
    f=io.BytesIO()
    with wave.open(f,"wb") as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000)
        w.writeframes(b"\x01\x00"*3000)
    return f.getvalue()

def test_n01_new_audio_and_explicit_new_transcript_atomic(tmp_path,monkeypatch):
    import hashlib
    from lite.profiles import Store
    monkeypatch.setattr("lite.profiles.shutil.which",lambda _: "ffmpeg")
    monkeypatch.setattr("lite.profiles.convert_reference",
        lambda src,dst,**kw:dst.write_bytes(wav_b() if src.read_bytes()==wav_b() else wav()))
    s=Settings(data_dir=tmp_path/"data",cache_dir=tmp_path/"cache",bin_dir=tmp_path/"bin",timeout=30)
    with TestClient(create_app(s)) as c:
        created=c.post("/profiles",data={"name":"Owned","ref_text":"Transcript A","consent":"owner-confirmed-local"},
            files={"ref_audio":("a.wav",wav(),"audio/wav")}).json()
        stable=created["id"]
        changed=c.put("/profiles/"+stable+"/audio",data={"ref_text":"New exact transcript B","consent":"owner-confirmed-local"},
            files={"ref_audio":("b.wav",wav_b(),"audio/wav")})
        assert changed.status_code==200,changed.text
        row=changed.json()
        assert row["id"]==stable and row["version"]==2
        assert row["ref_text"]=="New exact transcript B"
        assert row["consent"]=="owner-confirmed-local"
        assert row["ref_sha256"]==hashlib.sha256(wav_b()).hexdigest()
        assert row["source_sha256"]==hashlib.sha256(wav_b()).hexdigest()
        assert row["ref_sha256"]!=created["ref_sha256"]
        assert c.get("/profiles/"+stable).json()["ref_text"]=="New exact transcript B"
        assert Store(s).reference(row).read_bytes()==wav_b()
        assert Store(s).reference(created).read_bytes()==wav()

def test_n02_shared_cli_http_readiness_without_ffmpeg(tmp_path,monkeypatch,capsys):
    import lite.app as appmod
    import lite.preflight as climod
    s=Settings(data_dir=tmp_path/"data",cache_dir=tmp_path/"cache",bin_dir=tmp_path/"bin",timeout=30)
    fake=lambda settings:{"ready":True,"binary":True,"quant":{"verified":True},"gpu":{"verified":True},"issues":[]}
    monkeypatch.setattr(appmod,"preflight",fake)
    monkeypatch.setattr(climod,"preflight",fake)
    monkeypatch.setattr(appmod.shutil,"which",lambda _:None)
    monkeypatch.setattr(climod.shutil,"which",lambda _:None)
    monkeypatch.setattr(climod.Settings,"load",lambda:s)
    monkeypatch.setattr(climod.Licence,"status",lambda self:{"accepted":True,"provenance_reconciled":True})
    app=create_app(s)
    monkeypatch.setattr(app.state.licence,"status",lambda:{"accepted":True,"provenance_reconciled":True})
    with TestClient(app) as c:
        api=c.get("/lite/readiness").json()
    rc=climod.main()
    console=json.loads(capsys.readouterr().out)
    assert rc==1
    for key in ("runtime_ready","tts_ready","cloning_ready","ready","ffmpeg_installed"):
        assert api[key]==console[key],(key,api,console)
    assert api["runtime_ready"] is True and api["tts_ready"] is True
    assert api["cloning_ready"] is False and api["ready"] is False


def test_n01_explicit_same_words_and_legacy_no_text_compatibility(tmp_path,monkeypatch):
    import hashlib
    from lite.profiles import Store
    monkeypatch.setattr("lite.profiles.shutil.which",lambda _: "ffmpeg")
    monkeypatch.setattr("lite.profiles.convert_reference",lambda src,dst,**kw:dst.write_bytes(wav()))
    s=Settings(data_dir=tmp_path/"data",cache_dir=tmp_path/"cache",bin_dir=tmp_path/"bin",timeout=30)
    with TestClient(create_app(s)) as c:
        row=c.post("/profiles",data={"name":"Test","ref_text":"Old exact words","consent":"original"},
            files={"ref_audio":("a.wav",wav(),"audio/wav")}).json()
        url="/profiles/"+row["id"]+"/audio"
        explicit=c.put(url,data={"reuse_existing_transcript":"true","consent":"new-acknowledgement"},
            files={"ref_audio":("b.wav",wav(),"audio/wav")})
        assert explicit.status_code==200,explicit.text
        assert explicit.json()["ref_text"]=="Old exact words"
        assert explicit.json()["consent"]=="new-acknowledgement"
        assert explicit.json()["version"]==2
        legacy=c.put(url,files={"ref_audio":("c.wav",wav(),"audio/wav")})
        assert legacy.status_code==200,legacy.text
        assert legacy.json()["version"]==3 and legacy.json()["ref_text"]=="Old exact words"
        assert legacy.json()["consent"] is None
        assert c.put(url,data={"ref_text":"  "},
            files={"ref_audio":("d.wav",wav(),"audio/wav")}).status_code==422
        assert c.put(url,data={"ref_text":"different","reuse_existing_transcript":"true"},
            files={"ref_audio":("e.wav",wav(),"audio/wav")}).status_code==422
        assert c.get("/profiles/"+row["id"]).json()["version"]==3
        before=Store(s).reference(legacy.json()).read_bytes()
        assert before==wav()
        assert Store(s).reference(legacy.json()).exists()

def test_n02_full_readiness_truth_table(tmp_path,monkeypatch):
    import lite.app as appmod
    from lite.readiness import complete_readiness
    from lite.config import Settings
    s=Settings(data_dir=tmp_path/"data",cache_dir=tmp_path/"cache",bin_dir=tmp_path/"bin",timeout=30)
    engine={"ready":True,"gpu":{"verified":True},"quant":{"verified":True},"binary":True,"issues":[]}
    for runtime,terms,ffmpeg,expected in (
      (True,True,True,(True,True,True,True)),
      (True,True,False,(True,True,False,False)),
      (True,False,True,(True,False,False,False)),
      (False,True,True,(False,False,False,False)),
    ):
        status={"accepted":terms,"provenance_reconciled":terms}
        report=complete_readiness({**engine,"ready":runtime},status,store_healthy=True,ffmpeg_installed=ffmpeg)
        assert tuple(report[k] for k in ("runtime_ready","tts_ready","cloning_ready","ready"))==expected
        assert report["ffmpeg"]==report["ffmpeg_installed"]==ffmpeg
    # Quant GET continues reporting raw ENGINE readiness, not full licence gate.
    monkeypatch.setattr(appmod,"preflight",lambda settings:engine.copy())
    monkeypatch.setattr(appmod.shutil,"which",lambda _:None)
    app=create_app(s)
    monkeypatch.setattr(app.state.licence,"status",lambda:{"accepted":True,"provenance_reconciled":True})
    with TestClient(app) as c:
        assert c.get("/engines/omnivoice-gguf/quants").json()["ready"] is True
        result=c.get("/lite/readiness").json()
        assert result["ready"] is False and result["tts_ready"] is True
        assert c.get("/health").json()["ready"] is False


def test_n01_bad_audio_or_transcript_does_not_publish_mixed_version(tmp_path,monkeypatch):
    from lite.profiles import Store
    from lite.audio import AudioError
    monkeypatch.setattr("lite.profiles.shutil.which",lambda _: "ffmpeg")
    monkeypatch.setattr("lite.profiles.convert_reference",lambda src,dst,**kw:dst.write_bytes(wav()))
    s=Settings(data_dir=tmp_path/"data",cache_dir=tmp_path/"cache",bin_dir=tmp_path/"bin",timeout=30)
    store=Store(s)
    app=create_app(s,store=store)
    with TestClient(app) as c:
        first=c.post("/profiles",data={"name":"Owned","ref_text":"A","consent":"yes"},
           files={"ref_audio":("old.wav",wav(),"audio/wav")}).json()
        sid=first["id"]
        monkeypatch.setattr("lite.profiles.convert_reference",lambda *args,**kwargs:
                            (_ for _ in ()).throw(AudioError("Synthetic bad source")))
        bad=c.put("/profiles/"+sid+"/audio",data={"ref_text":"B","consent":"new"},
           files={"ref_audio":("new.wav",wav_b(),"audio/wav")})
        assert bad.status_code==422
        row=c.get("/profiles/"+sid).json()
        assert (row["id"],row["version"],row["ref_text"],row["ref_sha256"],row["consent"])==(
           sid,1,"A",first["ref_sha256"],"yes")
        assert store.reference(row).read_bytes()==wav()
        assert not (s.data_dir/"voices"/sid/"2").exists()

def test_n01_replacement_with_different_transcript_is_busy_during_speech(tmp_path,monkeypatch):
    import asyncio
    import httpx
    monkeypatch.setattr("lite.profiles.shutil.which",lambda _: "ffmpeg")
    monkeypatch.setattr("lite.profiles.convert_reference",lambda src,dst,**kw:dst.write_bytes(wav()))
    s=Settings(data_dir=tmp_path/"data",cache_dir=tmp_path/"cache",bin_dir=tmp_path/"bin",timeout=30)
    app=create_app(s)
    row=app.state.store.import_audio(name="Owned",ref_text="A",language="en",source=wav(),suffix=".wav")
    app.state.licence.is_ready=lambda:True
    entered=asyncio.Event()
    leave=asyncio.Event()
    async def native(**kwargs):
        entered.set()
        await leave.wait()
        assert kwargs["profile"]["ref_text"]=="A"
        return wav(),{"request_id":"fake","verification_status":"verified",
            "observed_native_backend":"Vulkan0","observed_device_identity":"NVIDIA GTX 1650"}
    app.state.runner.generate=native
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                   base_url="http://127.0.0.1:3900") as c:
            speaking=asyncio.create_task(c.post("/v1/audio/speech",json={
                "model":"omnivoice-gguf","voice":row["id"],"input":"Hello","speed":1.0}))
            await asyncio.wait_for(entered.wait(),timeout=4)
            blocked=await c.put("/profiles/"+row["id"]+"/audio",
                data={"ref_text":"Different B","consent":"new"},
                files={"ref_audio":("b.wav",wav_b(),"audio/wav")})
            assert blocked.status_code==409
            assert app.state.store.get(row["id"])["version"]==1
            leave.set()
            assert (await asyncio.wait_for(speaking,timeout=5)).status_code==200
    asyncio.run(exercise())
