"""Read-only legacy DB migration; defaults to dry run."""
import hashlib
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path
from .config import Settings
from .profiles import Store

def migrate(dbpath, voices, *, apply=False):
    dbpath = Path(dbpath).resolve(strict=True)
    voices = Path(voices).resolve(strict=True)
    if not dbpath.is_file() or not voices.is_dir():
        raise ValueError("Missing legacy source")
    with sqlite3.connect(dbpath.as_uri()+"?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(r) for r in db.execute("SELECT id, name, ref_audio_path, ref_text, language, kind FROM voice_profiles")]
    valid, invalid = [], []
    for r in rows:
        if r["kind"] != "clone":
            continue
        name = r["ref_audio_path"] or ""
        if not r["ref_text"] or not r["ref_text"].strip() or not name or Path(name).name != name:
            invalid.append({"id":r["id"],"reason":"Missing transcript or unsafe path"})
            continue
        try:
            p = (voices / name).resolve(strict=True)
            if not p.is_relative_to(voices) or p.suffix.lower() not in (".mp3",".wav"):
                raise ValueError()
            digest = hashlib.sha256(p.read_bytes()).hexdigest()
            valid.append((r,p,digest))
        except (ValueError,OSError):
            invalid.append({"id":r["id"],"reason":"Missing or unsafe audio"})
    summary = {"dry_run":not apply,"eligible":len(valid),"skipped":invalid,"migrated":0}
    if not apply or invalid:
        return summary
    store = Store(Settings.load())
    if store.db.exists():
        shutil.copy2(store.db, store.db.with_suffix(".backup-"+str(int(time.time()))))
    for r,p,digest in valid:
        if not r["id"] or "/" in r["id"] or "\\" in r["id"] or r["id"] in (".","..") or store.get(r["id"]):
            raise ValueError("Invalid or duplicate legacy ID")
        data = p.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Source changed")
        with store.connect() as db:
            db.execute("INSERT INTO profiles VALUES(?,?,?,?,?,?,?,?,?,?,?)",(
                r["id"],r["name"],r["language"] or "en",r["ref_text"],"","","",0,None,time.time(),time.time()))
        try:
            store.import_audio(name=r["name"],language=r["language"] or "en",
                               ref_text=r["ref_text"],source=data,suffix=p.suffix,profile_id=r["id"])
        except BaseException:
            with store.connect() as db:
                db.execute("DELETE FROM profiles WHERE id=?",(r["id"],))
            raise
        summary["migrated"]+=1
    return summary

if __name__=="__main__":
    print(json.dumps(migrate(os.environ["GGUF_LITE_LEGACY_DB"],os.environ["GGUF_LITE_LEGACY_VOICES"],
        apply=os.environ.get("GGUF_LITE_MIGRATE_APPLY")=="1"),indent=2))
