"""Fixed server-root migration preview and one selected voice import through real API."""
import hashlib
import io
import sqlite3
import wave
from pathlib import Path
from fastapi.testclient import TestClient
from lite.app import create_app
from lite.config import Settings
from lite.profiles import Store

def test_fixed_path_migration_without_browser_filesystem_paths(tmp_path,monkeypatch):
    root=tmp_path/"trusted";root.mkdir()
    voices=root/"voices";voices.mkdir()
    original=voices/"legacy.wav"
    with wave.open(str(original),"wb") as f:
        f.setnchannels(1);f.setsampwidth(2);f.setframerate(24000)
        f.writeframes(b"\x01\x00"*2400)
    db=root/"voices.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE voice_profiles(id TEXT,name TEXT,ref_audio_path TEXT,ref_text TEXT,language TEXT,kind TEXT)")
        conn.execute("INSERT INTO voice_profiles VALUES(?,?,?,?,?,?)",
                     ("known-voice","Owner voice","legacy.wav","Exact recording","en","clone"))
    before=hashlib.sha256(db.read_bytes()).hexdigest()
    monkeypatch.setenv("GGUF_LITE_LEGACY_ROOT",str(root))
    settings=Settings(data_dir=tmp_path/"new",cache_dir=tmp_path/"cache",bin_dir=tmp_path/"bin",timeout=30)
    with TestClient(create_app(settings)) as c:
        preview=c.get("/lite/migration/preview")
        assert preview.status_code==200,preview.text
        assert preview.json()["eligible"][0]["id"]=="known-voice"
        assert "ref_audio_path" not in preview.text and str(root) not in preview.text
        assert not Store(settings).get("known-voice")
        denied=c.post("/lite/migration/apply",json={"voice_id":"../attack"})
        assert denied.status_code==422
        imported=c.post("/lite/migration/apply",json={"voice_id":"known-voice"})
        assert imported.status_code==200,imported.text
        assert imported.json()["migrated"]==1
        assert Store(settings).get("known-voice")["ref_text"]=="Exact recording"
        second=c.post("/lite/migration/apply",json={"voice_id":"known-voice"})
        assert second.status_code==200 and second.json()["skipped"]==1
        assert hashlib.sha256(db.read_bytes()).hexdigest()==before
        assert original.is_file()
        # No public filepath selector supported.
        assert "legacy_path" not in c.get("/openapi.json").json()["paths"]["/lite/migration/apply"]["post"]["requestBody"]["content"]["application/json"]["schema"].get("properties",{})

def test_symlinked_source_root_is_rejected(tmp_path,monkeypatch):
    root=tmp_path/"trusted";root.mkdir()
    alias=tmp_path/"alias"
    try:alias.symlink_to(root,target_is_directory=True)
    except (OSError,NotImplementedError):return
    monkeypatch.setenv("GGUF_LITE_LEGACY_ROOT",str(alias))
    settings=Settings(data_dir=tmp_path/"new",cache_dir=tmp_path/"cache",bin_dir=tmp_path/"bin",timeout=30)
    with TestClient(create_app(settings)) as c:
        assert c.get("/lite/migration/preview").status_code==409
