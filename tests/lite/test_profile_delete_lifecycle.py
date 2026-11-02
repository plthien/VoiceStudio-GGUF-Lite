"""Deletion semantics are explicit, reversible on database failure and path-safe."""
import io
import sqlite3
import wave
from pathlib import Path
import pytest
from lite.config import Settings
from lite.profiles import Store,ProfileError

def wav():
    b=io.BytesIO()
    with wave.open(b,"wb") as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000);w.writeframes(b"\x04\x00"*2400)
    return b.getvalue()

@pytest.fixture
def example(tmp_path,monkeypatch):
    settings=Settings(data_dir=tmp_path/"owned",cache_dir=tmp_path/"cache",bin_dir=tmp_path/"bin",timeout=30)
    monkeypatch.setattr("lite.profiles.shutil.which",lambda name:"/fake/ffmpeg")
    monkeypatch.setattr("lite.profiles.convert_reference",lambda src,dst,**kw:dst.write_bytes(wav()))
    store=Store(settings)
    profile=store.import_audio(name="Owned",ref_text="Exactly spoken",language="en",suffix=".wav",source=wav())
    return store,profile,tmp_path

def test_purge_all_owned_versions_and_keep_legacy_outside(example):
    store,profile,base=example
    foreign=base/"legacy.wav";foreign.write_bytes(b"Legacy owner data")
    updated=store.import_audio(name="Updated",ref_text="Updated",language="en",suffix=".wav",source=wav(),profile_id=profile["id"])
    sid=profile["id"]
    assert (store.root/sid/"1"/"reference.wav").exists()
    assert (store.root/sid/"2"/"reference.wav").exists()
    store.delete(sid,active_id=None,purge=True)
    assert store.get(sid) is None
    assert not (store.root/sid).exists()
    assert foreign.read_bytes()==b"Legacy owner data"

def test_purge_database_failure_restores_reference_and_row(example):
    store,profile,_=example
    sid=profile["id"]
    with store.connect() as con:
        con.execute("""CREATE TRIGGER deny_delete BEFORE DELETE ON profiles
          BEGIN SELECT RAISE(ABORT,'synthetic DB failure'); END;""")
    with pytest.raises(sqlite3.IntegrityError):
        store.delete(sid,active_id=None,purge=True)
    assert store.get(sid) is not None
    assert (store.root/sid/"1"/"reference.wav").exists()
    assert not list(store.root.glob(".purge-*"))

def test_unsafe_profile_id_rejected_without_touching_other_data(example):
    store,profile,base=example
    external=base/"private";external.mkdir()
    (external/"audio.wav").write_bytes(b"private")
    with pytest.raises(ProfileError):
        store.delete("../private",active_id=None,purge=True)
    assert external.joinpath("audio.wav").read_bytes()==b"private"
    assert store.get(profile["id"]) is not None
