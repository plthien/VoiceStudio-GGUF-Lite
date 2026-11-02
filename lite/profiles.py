"""Lite-owned SQLite voice records and versioned audio, never touching legacy DB."""
from __future__ import annotations
import hashlib
import os
import re
import shutil
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path
from threading import RLock

from .audio import AudioError, convert_reference
from .config import Settings, require_owned_path

class ProfileError(ValueError):
    pass

class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.lock = RLock()
        self.root = settings.data_dir / "voices"
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = settings.data_dir / "lite.sqlite3"
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS profiles(
                id TEXT PRIMARY KEY, name TEXT NOT NULL, language TEXT NOT NULL,
                ref_text TEXT NOT NULL, ref_path TEXT NOT NULL, ref_sha256 TEXT NOT NULL,
                source_sha256 TEXT NOT NULL, version INTEGER NOT NULL,
                consent TEXT, created REAL NOT NULL, updated REAL NOT NULL)""")

    def connect(self):
        db = sqlite3.connect(self.db, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def get(self, profile_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
        return dict(row) if row else None

    def list(self) -> list[dict]:
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM profiles ORDER BY created, id")]

    def reference(self, profile: dict) -> Path:
        try:
            path = require_owned_path(self.root / profile["ref_path"], self.root)
            if hashlib.sha256(path.read_bytes()).hexdigest() != profile["ref_sha256"]:
                raise ValueError("Reference checksum mismatch")
            return path
        except (OSError, ValueError, KeyError) as exc:
            raise ProfileError("Stored reference is unavailable or untrusted") from exc

    def import_audio(self, *, name: str, ref_text: str, language: str,
                     source: bytes, suffix: str, profile_id: str | None = None,
                     consent: str | None = None) -> dict:
        if not name.strip() or len(name) > 128 or not ref_text.strip() or len(ref_text) > 4096:
            raise ProfileError("Name and explicit reference transcript are required")
        if suffix.lower() not in (".wav", ".mp3"):
            raise ProfileError("Reference must be WAV or MP3")
        if not 64 <= len(source) <= self.settings.max_ref_bytes:
            raise ProfileError("Invalid reference byte count")
        if not language or len(language) > 32:
            raise ProfileError("Invalid language")
        if profile_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}",profile_id):
            raise ProfileError("Unsafe voice identifier")
        with self.lock:
            existing = self.get(profile_id) if profile_id else None
            if profile_id is not None and existing is None:
                raise KeyError(profile_id)
            identity = profile_id or str(uuid.uuid4())
            version = (existing["version"] + 1) if existing else 1
            rel = Path(identity) / str(version)
            target_dir = self.root / rel
            target_dir.mkdir(parents=True, exist_ok=False)
            source_file = target_dir / ("original" + suffix.lower())
            normalized = target_dir / "reference.wav"
            try:
                source_file.write_bytes(source)
                import shutil as _shutil
                ffmpeg = _shutil.which("ffmpeg")
                if not ffmpeg:
                    raise ProfileError("FFmpeg unavailable")
                convert_reference(source_file, normalized, ffmpeg=ffmpeg,
                                  max_seconds=self.settings.max_ref_seconds)
                digest = hashlib.sha256(normalized.read_bytes()).hexdigest()
                src_digest = hashlib.sha256(source).hexdigest()
                now = time.time()
                with self.connect() as db:
                    if existing:
                        db.execute("""UPDATE profiles SET name=?, language=?, ref_text=?,
                            ref_path=?, ref_sha256=?, source_sha256=?, version=?, consent=?,
                            updated=? WHERE id=?""",
                            (name.strip(), language, ref_text, str(rel / "reference.wav"),
                             digest, src_digest, version, consent, now, identity))
                    else:
                        db.execute("""INSERT INTO profiles VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                            (identity, name.strip(), language, ref_text,
                             str(rel / "reference.wav"), digest, src_digest,
                             version, consent, now, now))
            except BaseException:
                shutil.rmtree(target_dir, ignore_errors=True)
                raise
            return self.get(identity)

    def update(self, profile_id: str, updates: dict) -> dict:
        allowed = {"name", "ref_text", "language", "consent"}
        if not updates or set(updates) - allowed:
            raise ProfileError("Only name, ref_text, language and consent may be edited")
        if any(not isinstance(v, str) or len(v) > (4096 if k == "ref_text" else 128) or
               (k != "consent" and not v.strip()) for k, v in updates.items()):
            raise ProfileError("Invalid profile fields")
        with self.lock, self.connect() as db:
            if not db.execute("SELECT 1 FROM profiles WHERE id=?", (profile_id,)).fetchone():
                raise KeyError(profile_id)
            columns = ", ".join(f"{key}=?" for key in updates)
            db.execute(f"UPDATE profiles SET {columns}, updated=? WHERE id=?",
                       [*updates.values(), time.time(), profile_id])
        return self.get(profile_id)

    def delete(self, profile_id: str, *, active_id: str | None) -> None:
        with self.lock, self.connect() as db:
            if active_id == profile_id:
                raise ProfileError("Profile is currently in use")
            if not db.execute("SELECT 1 FROM profiles WHERE id=?", (profile_id,)).fetchone():
                raise KeyError(profile_id)
            db.execute("DELETE FROM profiles WHERE id=?", (profile_id,))
        # Old version files intentionally retained until an explicit offline cleanup.
