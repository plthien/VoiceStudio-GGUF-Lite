"""Actual persistence/FFmpeg/upload behavior, not code-presence tests."""
import hashlib
import io
import sqlite3
import wave
from pathlib import Path
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
from lite.config import Settings
from lite.profiles import Store, ProfileError
from lite.audio import wav_info, AudioError
from lite.app import create_app

def sample_wav(rate=24000, channels=1):
    buf=io.BytesIO()
    with wave.open(buf,"wb") as out:
        out.setnchannels(channels);out.setsampwidth(2);out.setframerate(rate)
        out.writeframes(b"\x01\x00"*rate*channels)
    return buf.getvalue()

@pytest.fixture
def fixture(tmp_path, monkeypatch):
    settings=Settings(data_dir=tmp_path/"data",cache_dir=tmp_path/"models",
                      bin_dir=tmp_path/"bin",timeout=30)
    monkeypatch.setattr("lite.profiles.shutil.which",lambda name: "/fake/ffmpeg")
    def fake_convert(src,dst,*,ffmpeg,max_seconds):
        dst.write_bytes(sample_wav())
        return wav_info(dst.read_bytes())
    monkeypatch.setattr("lite.profiles.convert_reference",fake_convert)
    return Store(settings),settings

def test_stable_id_and_multiple_versions(fixture):
    store,_=fixture
    p=store.import_audio(name="Ink Explainer",ref_text="Accurate text",language="en",
        suffix=".mp3",source=b"x"*256)
    same=store.import_audio(name="New display name",ref_text="New transcript",language="en",
        suffix=".wav",source=b"y"*256,profile_id=p["id"])
    assert same["id"]==p["id"]
    assert same["version"]==2
    assert same["ref_text"]=="New transcript"
    assert store.reference(same).exists()
    assert store.get(same["id"])==same

def test_original_reference_unchanged(fixture):
    store,_=fixture
    original=b"x"*256
    p=store.import_audio(name="Voice",ref_text="Hello",language="en",suffix=".mp3",source=original)
    original_path=store.root / p["id"] / "1" / "original.mp3"
    assert original_path.read_bytes()==original
    assert p["source_sha256"]==hashlib.sha256(original).hexdigest()

def test_missing_transcript_and_bad_format(fixture):
    store,_=fixture
    for args in ({"ref_text":""},{"suffix":".exe"},{"source":b"short"}):
        params={"name":"Voice","ref_text":"Hello","language":"en",
                "suffix":".wav","source":b"x"*256}
        params.update(args)
        with pytest.raises(ProfileError):
            store.import_audio(**params)

def test_profile_delete_during_active_generation_blocked(fixture):
    store,_=fixture
    row=store.import_audio(name="Voice",ref_text="Hello",language="en",
        suffix=".wav",source=b"x"*256)
    with pytest.raises(ProfileError):
        store.delete(row["id"],active_id=row["id"])
    assert store.get(row["id"]) is not None
    store.delete(row["id"],active_id=None)
    assert store.get(row["id"]) is None

def test_reference_tamper_fails_closed(fixture):
    store,_=fixture
    row=store.import_audio(name="Voice",ref_text="Hello",language="en",
        suffix=".wav",source=b"x"*256)
    store.reference(row).write_bytes(b"tampered")
    with pytest.raises(ProfileError):
        store.reference(row)

def test_duplicate_names_are_not_id_aliases(fixture):
    store,_=fixture
    ids=[store.import_audio(name="Same",ref_text="Test",language="en",
           suffix=".wav",source=b"x"*256)["id"] for _ in range(2)]
    assert len(set(ids))==2
    assert len(store.list())==2
    assert store.get("Same") is None

def test_upload_route_transcript(fixture):
    store,settings=fixture
    app=create_app(settings=settings,store=store)
    with TestClient(app) as c:
        response=c.post("/profiles",data={"name":"Ink","ref_text":"Exactly spoken",
                        "language":"en"},
                        files={"ref_audio":("clip.mp3",b"x"*256,"audio/mpeg")})
        assert response.status_code==200, response.text
        profile=response.json()
        assert profile["ref_text"]=="Exactly spoken"
        voices=c.get("/v1/audio/voices").json()
        assert voices[0]["voice_id"]==profile["id"]
        assert c.post("/v1/audio/speech",json={"model":"omnivoice-gguf",
               "voice":"Ink","input":"Hello"}).status_code==404

def test_invalid_pcm_rejected():
    with pytest.raises(AudioError):
        wav_info(sample_wav(rate=48000))
    with pytest.raises(AudioError):
        wav_info(sample_wav(channels=2))
    with pytest.raises(AudioError):
        wav_info(sample_wav()[:-200])

def test_update_validation(fixture):
    store,_=fixture
    p=store.import_audio(name="Voice",ref_text="Hello",language="en",
                         suffix=".wav",source=b"x"*256)
    with pytest.raises(ProfileError):
        store.update(p["id"],{"invalid":"test"})
    with pytest.raises(ProfileError):
        store.update(p["id"],{"name":" "})
    assert store.update(p["id"],{"name":"Renamed"})["name"]=="Renamed"
