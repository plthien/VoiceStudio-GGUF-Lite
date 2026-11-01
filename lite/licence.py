"""Explicit, Lite-owned acceptance; original licence evidence is never rewritten."""
from __future__ import annotations
import hashlib
import json
import sqlite3
import time
from .config import ROOT, REPO_ID, MODEL_REVISION

def disclosure() -> dict:
    document = json.loads((ROOT / "backend/config/model_licenses.json").read_text(encoding="utf-8"))
    record = next(x for x in document["models"] if x["id"] == REPO_ID)
    return record

def terms() -> dict:
    record = disclosure()
    return {"model": REPO_ID, "model_revision": MODEL_REVISION,
            "evidence_revision": record["revision"], "disclosure": record}

def fingerprint() -> str:
    blob = json.dumps(terms(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "lite-v1:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()

class Licence:
    def __init__(self, settings):
        self.dbpath = settings.data_dir / "lite.sqlite3"
        with self.db() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS licence_acceptance(
                repo_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                action TEXT NOT NULL, changed REAL NOT NULL)""")

    def db(self):
        return sqlite3.connect(self.dbpath, timeout=10)

    def status(self) -> dict:
        f = fingerprint()
        with self.db() as db:
            row = db.execute("SELECT fingerprint, action FROM licence_acceptance WHERE repo_id=?",
                             (REPO_ID,)).fetchone()
        accepted = bool(row and row == (f, "accepted"))
        record = disclosure()
        reconciled = record["revision"] == MODEL_REVISION
        return {"repo_id": REPO_ID, "fingerprint": f, "accepted": accepted,
                "state": "accepted" if accepted else
                         "terms_updated" if row and row[0] != f else
                         "withdrawn" if row and row[1] == "withdrawn" else "not_accepted",
                "provenance_reconciled": reconciled,
                "commercial_cleared": False, "model_revision": MODEL_REVISION,
                "evidence_revision": record["revision"]}

    def details(self) -> dict:
        return {"info": disclosure(), "acceptance": self.status()}

    def change(self, *, expected: str, action: str) -> dict:
        if expected != fingerprint() or action not in ("accepted", "withdrawn"):
            raise ValueError("Terms changed or invalid action")
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO licence_acceptance VALUES(?,?,?,?)",
                       (REPO_ID, expected, action, time.time()))
        return self.status()

    def is_ready(self) -> bool:
        status = self.status()
        return status["accepted"] and status["provenance_reconciled"]
