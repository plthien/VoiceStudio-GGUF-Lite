"""Explicit licence acceptance + optional independently reviewed provenance receipt.

The legacy disclosure is preserved. A reviewed receipt is supplied on the
local host only after a person validates exact pinned source bytes/terms.
No retrieval or auto-accept occurs at startup or generation.
"""
from __future__ import annotations
import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from .config import ROOT, REPO_ID, MODEL_REVISION

def disclosure() -> dict:
    document=json.loads((ROOT/"lite/resources/model_notice.json").read_text(encoding="utf-8"))
    record=document["model"]
    if record["id"]!=REPO_ID: raise ValueError("Pinned GGUF notice mismatch")
    return record

def receipt(settings) -> dict | None:
    """Validate local evidence receipt content, not merely an unchecked flag."""
    path=settings.data_dir/"provenance"/"review.json"
    document=settings.data_dir/"provenance"/"README.md"
    if not path.is_file() or not document.is_file():
        return None
    try:
        row=json.loads(path.read_text(encoding="utf-8"))
        expected_url="https://huggingface.co/"+REPO_ID+"/blob/"+MODEL_REVISION+"/README.md"
        if (row.get("model_revision")!=MODEL_REVISION or
            row.get("repo_id")!=REPO_ID or
            row.get("evidence_url")!=expected_url or
            row.get("model_licence")!="CC-BY-NC-4.0" or
            row.get("commercial_inference")!="restricted" or
            not row.get("reviewed_by") or not row.get("reviewed_at") or
            not re.fullmatch(r"[a-f0-9]{64}",row.get("document_sha256","")) or
            hashlib.sha256(document.read_bytes()).hexdigest()!=row["document_sha256"]):
            return None
        return {key:row[key] for key in (
            "model_revision","repo_id","evidence_url","model_licence",
            "commercial_inference","reviewed_by","reviewed_at","document_sha256")}
    except (OSError,ValueError,KeyError,TypeError):
        return None

def terms(settings=None) -> dict:
    record=disclosure()
    return {"model":REPO_ID,"model_revision":MODEL_REVISION,
            "legacy_evidence_revision":record["revision"],
            "reviewed_provenance":receipt(settings) if settings else None,
            "disclosure":record}

def fingerprint(settings=None) -> str:
    blob=json.dumps(terms(settings),sort_keys=True,separators=(",",":"),ensure_ascii=False)
    return "lite-v1:"+hashlib.sha256(blob.encode("utf-8")).hexdigest()

class Licence:
    def __init__(self,settings):
        self.settings=settings
        self.dbpath=settings.data_dir/"lite.sqlite3"
        settings.data_dir.mkdir(parents=True,exist_ok=True)
        with self.db() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS licence_acceptance(
                repo_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                action TEXT NOT NULL, changed REAL NOT NULL)""")

    def db(self):
        return sqlite3.connect(self.dbpath,timeout=10)

    def status(self) -> dict:
        f=fingerprint(self.settings)
        with self.db() as db:
            row=db.execute("SELECT fingerprint,action FROM licence_acceptance WHERE repo_id=?",
                           (REPO_ID,)).fetchone()
        accepted=bool(row and row==(f,"accepted"))
        info=disclosure()
        reviewed=receipt(self.settings)
        reconciled=info["revision"]==MODEL_REVISION or reviewed is not None
        return {"repo_id":REPO_ID,"fingerprint":f,"accepted":accepted,
                "state":"accepted" if accepted else
                        "terms_updated" if row and row[0]!=f else
                        "withdrawn" if row and row[1]=="withdrawn" else "not_accepted",
                "provenance_reconciled":reconciled,
                "provenance_review":reviewed,
                "commercial_cleared":False,"model_revision":MODEL_REVISION,
                "evidence_revision":info["revision"]}

    def details(self) -> dict:
        return {"info":disclosure(),"reviewed_terms":terms(self.settings),
                "acceptance":self.status()}

    def change(self,*,expected:str,action:str) -> dict:
        if expected!=fingerprint(self.settings) or action not in ("accepted","withdrawn"):
            raise ValueError("Terms changed or invalid action")
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO licence_acceptance VALUES(?,?,?,?)",
                       (REPO_ID,expected,action,time.time()))
        return self.status()

    def is_ready(self) -> bool:
        status=self.status()
        return status["accepted"] and status["provenance_reconciled"]
