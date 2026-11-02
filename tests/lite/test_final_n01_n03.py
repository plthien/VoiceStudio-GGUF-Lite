"""N01/N02/N03 final re-audit regressions. Preserve pre-fix red evidence."""
import io
import json
import wave
from fastapi.testclient import TestClient
from lite.app import create_app
from lite.config import Settings
from lite import preflight as cli

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
    monkeypatch.setattr("lite.profiles.convert_reference",lambda src,dst,**kw:dst.write_bytes(wav()))
    s=Settings(data_dir=tmp_path/"data",cache_dir=tmp_path/"cache",bin_dir=tmp_path/"bin",timeout=30)
    with TestClient(create_app(s)) as c:
        created=c.post("/profiles",data={"name":"Owned","ref_text":"Transcript A","consent":"owner-confirmed-local"},
            files={"ref_audio":("a.wav",wav(),"audio/wav")}).json()
        stable=created["id"]
        changed=c.put("/profiles/"+stable+"/audio",data={"ref_text":"New exact transcript B","consent":"owner-confirmed-local"},
            files={"ref_audio":("b.wav",wav(),"audio/wav")})
        assert changed.status_code==200,changed.text
        row=changed.json()
        assert row["id"]==stable and row["version"]==2
        assert row["ref_text"]=="New exact transcript B"
        assert row["consent"]=="owner-confirmed-local"
        assert row["ref_sha256"]==hashlib.sha256(wav()).hexdigest()
        assert c.get("/profiles/"+stable).json()["ref_text"]=="New exact transcript B"
        assert Store(s).reference(row).read_bytes()==wav()

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
