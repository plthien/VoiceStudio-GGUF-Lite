"""FastAPI same-origin UI and privacy regressions; no real model/GPU required."""
import hashlib
import io
import json
import sqlite3
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from lite.app import create_app
from lite.config import Settings,REPO_ID,MODEL_REVISION
from lite.licence import Licence
from lite.profiles import Store

def wav():
    buf=io.BytesIO()
    with wave.open(buf,"wb") as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000);w.writeframes(b"\x01\x00"*2400)
    return buf.getvalue()

@pytest.fixture
def local(tmp_path):
    settings=Settings(data_dir=tmp_path/"owned",cache_dir=tmp_path/"models",
                      bin_dir=tmp_path/"bin",timeout=30)
    with TestClient(create_app(settings)) as client:
        yield client,settings

def test_new_host_creates_owned_license_db_but_never_auto_accepts(local):
    client,s=local
    assert (s.data_dir/"lite.sqlite3").exists()
    response=client.get("/models/licenses/acceptance/"+REPO_ID)
    assert response.status_code==200
    assert response.json()["accepted"] is False
    assert response.json()["commercial_cleared"] is False

def test_allowlisted_ui_is_local_csp_and_does_not_expose_files(local):
    c,s=local
    root=c.get("/",follow_redirects=False)
    assert root.status_code==307 and root.headers["location"]=="/ui"
    index=c.get("/ui")
    assert index.status_code==200 and "Voice Cloning" in index.text
    assert index.headers["x-content-type-options"]=="nosniff"
    assert "default-src 'none'" in index.headers["content-security-policy"]
    for p,mime in (("app.css","text/css"),("app.js","text/javascript"),("api.js","text/javascript")):
        result=c.get("/ui/assets/"+p)
        assert result.status_code==200
        assert mime in result.headers["content-type"]
    for p in ("../../lite.sqlite3","runtime/bin/secret","backend/private.json","unknown.js"):
        result=c.get("/ui/assets/"+p)
        assert result.status_code in (404,422)
    assert c.get("/ui",headers={"Host":"evil.example"}).status_code==403

def test_status_cheap_no_multi_gb_preflight(local,monkeypatch):
    c,s=local
    def fail(*_): raise AssertionError("sha model scan called from /lite/status")
    monkeypatch.setattr("lite.app.preflight",fail)
    assert c.get("/lite/status").json()["voice_count"]==0
    assert c.get("/lite/status").json()["model"]=="omnivoice-gguf"

def test_reference_hash_verified_preview_and_purge(local,monkeypatch):
    c,s=local
    def normalized(src,dst,*,ffmpeg,max_seconds):
        dst.write_bytes(wav())
    monkeypatch.setattr("lite.profiles.shutil.which",lambda x:"/fake/ffmpeg")
    monkeypatch.setattr("lite.profiles.convert_reference",normalized)
    result=c.post("/profiles",data={"name":"Owned","language":"en","ref_text":"Exact words",
            "consent":"owner-confirmed-local"},
        files={"ref_audio":("reference.wav",wav(),"audio/wav")})
    assert result.status_code==200,result.text
    created=result.json()
    assert created["consent"]=="owner-confirmed-local"
    sid=created["id"]
    preview=c.get("/profiles/"+sid+"/reference")
    assert preview.status_code==200 and preview.content==wav()
    assert preview.headers["cache-control"]=="no-store"
    path=Store(s).reference(created)
    path.write_bytes(b"tampered")
    assert c.get("/profiles/"+sid+"/reference").status_code==409
    path.write_bytes(wav())
    assert c.delete("/profiles/"+sid+"?purge=true").status_code==200
    assert not (s.data_dir/"voices"/sid).exists()
    assert c.get("/profiles/"+sid).status_code==404

def test_default_delete_retains_files_and_reports_policy(local,monkeypatch):
    c,s=local
    monkeypatch.setattr("lite.profiles.shutil.which",lambda x:"ffmpeg")
    monkeypatch.setattr("lite.profiles.convert_reference",lambda src,dst,**k:dst.write_bytes(wav()))
    p=c.post("/profiles",data={"name":"Retain","ref_text":"A transcript","language":"en"},
        files={"ref_audio":("r.wav",wav(),"audio/wav")}).json()
    assert c.delete("/profiles/"+p["id"]).json()["owned_reference_files_purged"] is False
    assert (s.data_dir/"voices"/p["id"]/"1"/"reference.wav").is_file()

def test_cross_site_and_body_size_still_rejected(local):
    c,s=local
    assert c.post("/lite/migration/apply",json={"voice_id":"a"},headers={
        "Origin":"http://attacker.example"}).status_code==403
    assert c.post("/lite/migration/apply",json={"voice_id":"../secret"}).status_code==422
    assert c.get("/lite/migration/preview").status_code==409
