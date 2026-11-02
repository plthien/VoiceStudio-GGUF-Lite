"""Atomic, read-only external legacy voice migration. Dry run is side-effect free."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from .audio import convert_reference
from .config import Settings
from .profiles import Store

def _digest(path:Path)->str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda:f.read(1048576),b""):
            h.update(block)
    return h.hexdigest()

def _rows(dbpath:Path):
    with sqlite3.connect(dbpath.as_uri()+"?mode=ro&immutable=1",uri=True) as db:
        db.row_factory=sqlite3.Row
        return [dict(x) for x in db.execute(
            "SELECT id, name, ref_audio_path, ref_text, language, kind FROM voice_profiles")]

def _validate(rows,voices:Path,only_id):
    valid,invalid=[],[]
    for row in rows:
        if row["kind"]!="clone" or (only_id and row["id"]!=only_id):
            continue
        identity=row["id"]
        name=row["ref_audio_path"] or ""
        if not (isinstance(identity,str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}",identity)
                and name and Path(name).name==name and name not in (".","..")
                and isinstance(row["ref_text"],str) and row["ref_text"].strip()
                and isinstance(row["name"],str) and row["name"].strip()):
            invalid.append({"id":identity,"reason":"Invalid ID/name/transcript/reference path"})
            continue
        try:
            src=voices/name
            if src.is_symlink() or src.suffix.lower() not in (".wav",".mp3"):
                raise ValueError()
            real=src.resolve(strict=True)
            if not real.is_relative_to(voices) or not real.is_file() or real.stat().st_size<64:
                raise ValueError()
            valid.append((row,real,_digest(real),real.stat().st_size))
        except (OSError,ValueError):
            invalid.append({"id":identity,"reason":"Unsafe/corrupt/missing reference"})
    if only_id and not valid and not invalid:
        invalid.append({"id":only_id,"reason":"Requested voice does not exist"})
    ids=[r["id"] for r,_,_,_ in valid]
    if len(set(ids))!=len(ids):
        invalid.append({"id":None,"reason":"Duplicate target profile IDs"})
    return valid,invalid

def migrate(dbpath,voices,*,apply=False,only_id=None,settings=None):
    dbpath=Path(dbpath).resolve(strict=True)
    voices=Path(voices).resolve(strict=True)
    if not dbpath.is_file() or not voices.is_dir():
        raise ValueError("Missing read-only legacy source")
    original_db=_digest(dbpath)
    rows=_rows(dbpath)
    valid,invalid=_validate(rows,voices,only_id)
    result={"dry_run":not apply,"attempted":len(valid)+len(invalid),
            "eligible":len(valid),"invalid":invalid,"migrated":0,"skipped":0,
            "failed":len(invalid),"rolled_back":False,"source_unchanged":False,
            "projected_bytes":sum(item[3] for item in valid)}
    if not apply:
        result["source_unchanged"]=_digest(dbpath)==original_db
        return result
    if invalid:
        result["source_unchanged"]=_digest(dbpath)==original_db
        return result
    settings=settings or Settings.load()
    # Stage everything and perform FFmpeg before touching existing profile DB.
    settings.data_dir.mkdir(parents=True,exist_ok=True)
    staging=Path(tempfile.mkdtemp(prefix=".migrate-stage-",dir=settings.data_dir))
    published=[]
    conn=None
    try:
        staged=[]
        for row,source,digest,size in valid:
            if source.stat().st_size!=size or _digest(source)!=digest:
                raise ValueError("Legacy source changed during migration")
            dest=staging/row["id"]/"1"
            dest.mkdir(parents=True)
            original=dest/("original"+source.suffix.lower())
            shutil.copyfile(source,original)
            if _digest(original)!=digest:
                raise ValueError("Staged reference hash mismatch")
            normalized=dest/"reference.wav"
            ffmpeg=shutil.which("ffmpeg")
            if not ffmpeg:
                raise RuntimeError("FFmpeg unavailable")
            convert_reference(original,normalized,ffmpeg=ffmpeg,max_seconds=settings.max_ref_seconds)
            staged.append((row,dest,digest,_digest(normalized)))
        if _digest(dbpath)!=original_db:
            raise ValueError("Legacy DB changed during migration")
        store=Store(settings)
        conn=store.connect()
        conn.execute("BEGIN IMMEDIATE")
        timestamp=time.time()
        for row,src,source_hash,wav_hash in staged:
            identity=row["id"]
            existing=conn.execute("SELECT * FROM profiles WHERE id=?",(identity,)).fetchone()
            if existing:
                if (existing["source_sha256"]==source_hash and existing["ref_text"]==row["ref_text"]):
                    result["skipped"]+=1
                    continue
                raise ValueError("Existing Lite profile ID collision: "+identity)
            target=store.root/identity/"1"
            if target.exists():
                raise ValueError("Orphan voice directory collision: "+identity)
            target.parent.mkdir(parents=True,exist_ok=True)
            os.replace(src,target)
            published.append(target)
            conn.execute("INSERT INTO profiles VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (identity,row["name"],row["language"] or "en",row["ref_text"],
                 str(Path(identity)/"1"/"reference.wav"),wav_hash,source_hash,1,
                 None,timestamp,timestamp))
        if _digest(dbpath)!=original_db:
            raise ValueError("Legacy DB changed before commit")
        conn.commit()
        result["migrated"]=len(published)
    except BaseException as exc:
        if conn:
            conn.rollback()
        for path in reversed(published):
            shutil.rmtree(path,ignore_errors=True)
        result["rolled_back"]=bool(published)
        result["failed"]+=1
        result["errors"]=[type(exc).__name__+": "+str(exc)]
    finally:
        if conn:
            conn.close()
        shutil.rmtree(staging,ignore_errors=True)
        result["source_unchanged"]=_digest(dbpath)==original_db
    return result

def main():
    report=migrate(os.environ["GGUF_LITE_LEGACY_DB"],
        os.environ["GGUF_LITE_LEGACY_VOICES"],
        apply=os.environ.get("GGUF_LITE_MIGRATE_APPLY")=="1",
        only_id=os.environ.get("GGUF_LITE_MIGRATE_VOICE_ID") or None)
    print(json.dumps(report,indent=2))
    return 2 if report["failed"] or not report["source_unchanged"] else 0

if __name__=="__main__":
    raise SystemExit(main())

