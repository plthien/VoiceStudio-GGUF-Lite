"""External SQLite fixture: dry-run, atomic migration, rollback and idempotence."""
import hashlib
import io
import sqlite3
import wave
from pathlib import Path
import pytest
from lite.config import Settings
from lite.migrate import migrate
from lite.profiles import Store

def wav():
    buf=io.BytesIO()
    with wave.open(buf,"wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x01\x00"*2400)
    return buf.getvalue()

def setup(tmp_path,items):
    db=tmp_path/"legacy.sqlite3"
    voices=tmp_path/"old_voices"
    voices.mkdir()
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE voice_profiles(id TEXT,name TEXT,ref_audio_path TEXT,ref_text TEXT,language TEXT,kind TEXT)")
        for identity,text in items:
            name=identity+".wav"
            (voices/name).write_bytes(wav())
            con.execute("INSERT INTO voice_profiles VALUES(?,?,?,?,?,?)",
                        (identity,"Ink Explainer",name,text,"en","clone"))
    settings=Settings(data_dir=tmp_path/"lite",bin_dir=tmp_path/"bin",
                      cache_dir=tmp_path/"models",timeout=30)
    return db,voices,settings

def test_dry_run_is_read_only_and_does_not_create_target(tmp_path):
    db,voices,settings=setup(tmp_path,[("ink","This is an exact transcript")])
    before=hashlib.sha256(db.read_bytes()).hexdigest()
    result=migrate(db,voices,settings=settings)
    assert result["eligible"]==1 and result["migrated"]==0 and result["dry_run"]
    assert result["source_unchanged"]
    assert hashlib.sha256(db.read_bytes()).hexdigest()==before
    assert not settings.data_dir.exists()

def test_invalid_second_profile_does_not_partially_publish(tmp_path):
    db,voices,settings=setup(tmp_path,[("ink","valid"),("bad","not blank")])
    with sqlite3.connect(db) as con:
        con.execute("UPDATE voice_profiles SET ref_text='' WHERE id='bad'")
    result=migrate(db,voices,settings=settings,apply=True)
    assert result["failed"]==1 and result["migrated"]==0
    assert not settings.data_dir.exists()

def test_missing_selected_id_is_error_and_not_success(tmp_path):
    db,voices,settings=setup(tmp_path,[("ink","valid")])
    result=migrate(db,voices,settings=settings,only_id="missing",apply=True)
    assert result["failed"]==1 and result["migrated"]==0

def test_valid_apply_keeps_id_ref_text_and_source_and_reruns(tmp_path):
    db,voices,settings=setup(tmp_path,[("ink","Original exact transcript")])
    before=hashlib.sha256(db.read_bytes()).hexdigest()
    original=(voices/"ink.wav").read_bytes()
    result=migrate(db,voices,settings=settings,apply=True)
    assert result["migrated"]==1 and result["failed"]==0,result
    row=Store(settings).get("ink")
    assert row["ref_text"]=="Original exact transcript"
    assert row["source_sha256"]==hashlib.sha256(original).hexdigest()
    assert row["version"]==1 and row["ref_path"]=="ink/1/reference.wav"
    assert result["source_unchanged"] and hashlib.sha256(db.read_bytes()).hexdigest()==before
    assert (voices/"ink.wav").read_bytes()==original
    again=migrate(db,voices,settings=settings,apply=True)
    assert again["migrated"]==0 and again["skipped"]==1 and again["failed"]==0

def test_ffmpeg_error_rolls_back_batch(tmp_path,monkeypatch):
    db,voices,settings=setup(tmp_path,[("ink","valid"),("two","valid too")])
    def fail(*args,**kwargs):
        raise RuntimeError("synthetic FFmpeg failure")
    monkeypatch.setattr("lite.migrate.convert_reference",fail)
    result=migrate(db,voices,settings=settings,apply=True)
    assert result["failed"] and result["migrated"]==0
    assert not list((settings.data_dir/"voices").glob("*/1")) if (settings.data_dir/"voices").exists() else True

def test_reference_change_before_commit_is_rejected(tmp_path,monkeypatch):
    db,voices,settings=setup(tmp_path,[("ink","valid")])
    def mutate(original,target,**kwargs):
        (voices/"ink.wav").write_bytes(wav()+b"changed")
        target.write_bytes(wav())
    monkeypatch.setattr("lite.migrate.convert_reference",mutate)
    report=migrate(db,voices,settings=settings,apply=True)
    assert report["failed"]==1 and report["migrated"]==0
