"""Nano-owned explicit read-only import for compatible legacy profile stores."""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from pathlib import Path

from .profiles import Store


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def legacy_sources() -> tuple[Path, Path]:
    raw = os.environ.get("CHATTERBOX_NANO_LEGACY_ROOT")
    if not raw:
        raise ValueError("CHATTERBOX_NANO_LEGACY_ROOT_NOT_CONFIGURED")
    root = Path(raw).expanduser()
    if root.is_symlink():
        raise ValueError("LEGACY_ROOT_UNTRUSTED")
    root = root.resolve(strict=True)
    if not root.is_dir() or (os.name == "nt" and str(root).startswith("\\\\")):
        raise ValueError("LEGACY_ROOT_UNTRUSTED")
    database = root / os.environ.get("CHATTERBOX_NANO_LEGACY_DB", "data/lite.sqlite3")
    voices = root / os.environ.get("CHATTERBOX_NANO_LEGACY_VOICES", "data/voices")
    for source in (database, voices):
        if source.is_symlink() or not source.resolve(strict=True).is_relative_to(root):
            raise ValueError("LEGACY_PATH_UNTRUSTED")
    if not database.is_file() or not voices.is_dir():
        raise ValueError("LEGACY_SOURCE_UNAVAILABLE")
    if any(database.with_name(database.name + suffix).exists() for suffix in ("-wal", "-shm")):
        raise ValueError("LEGACY_SOURCE_ACTIVE")
    return database.resolve(), voices.resolve()


def _rows(database: Path) -> list[dict]:
    with sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True) as db:
        db.row_factory = sqlite3.Row
        columns = {row[1] for row in db.execute("PRAGMA table_info(profiles)")}
        required = {"id", "name", "language", "ref_text", "ref_path", "ref_sha256",
                    "source_sha256", "version", "consent"}
        if not required <= columns:
            raise ValueError("LEGACY_SCHEMA_UNSUPPORTED")
        return [dict(row) for row in db.execute(
            "SELECT id,name,language,ref_text,ref_path,ref_sha256,source_sha256,version,consent FROM profiles")]


def _eligible(row: dict, voices: Path) -> tuple[Path | None, str | None]:
    if (not isinstance(row.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", row["id"])
            or not isinstance(row.get("name"), str) or not row["name"].strip()
            or row.get("language") != "en" or row.get("consent") != "confirmed"
            or not isinstance(row.get("ref_text"), str) or not row["ref_text"].strip()
            or type(row.get("version")) is not int or row["version"] < 1):
        return None, "PROFILE_METADATA_OR_CONSENT_INVALID"
    try:
        relative = Path(row["ref_path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError()
        source = voices / relative
        if source.is_symlink():
            raise ValueError()
        source = source.resolve(strict=True)
        if not source.is_file() or not source.is_relative_to(voices.resolve()):
            raise ValueError()
        if _hash(source) != row["ref_sha256"]:
            return None, "REFERENCE_CHANGED"
        return source, None
    except (OSError, KeyError, TypeError, ValueError):
        return None, "REFERENCE_UNTRUSTED"


def preview(database: Path, voices: Path) -> dict:
    eligible, invalid = [], []
    for row in _rows(database):
        source, reason = _eligible(row, voices)
        if source is None:
            invalid.append({"id": row.get("id"), "reason": reason})
        else:
            eligible.append({"id": row["id"], "name": row["name"], "language": "en",
                             "source_version": row["version"], "state": "eligible"})
    return {"configured": True, "eligible": eligible, "invalid": invalid}


def apply(database: Path, voices: Path, settings, voice_id: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", voice_id):
        raise ValueError("INVALID_LEGACY_PROFILE_ID")
    database_hash = _hash(database)
    matching = [row for row in _rows(database) if row["id"] == voice_id]
    if len(matching) != 1:
        raise ValueError("LEGACY_PROFILE_NOT_FOUND")
    row = matching[0]
    source, reason = _eligible(row, voices)
    if source is None:
        raise ValueError(reason)
    source_hash = _hash(source)
    target = Store(settings)
    if target.get(voice_id):
        raise ValueError("PROFILE_ID_CONFLICT")
    data = source.read_bytes()
    if _hash(database) != database_hash or _hash(source) != source_hash:
        raise ValueError("LEGACY_SOURCE_CHANGED")
    imported = target.import_audio(name=row["name"], ref_text=row["ref_text"], language="en",
        source=data, suffix=".wav", profile_id=voice_id, consent="confirmed", create_with_id=True)
    try:
        if _hash(database) != database_hash or _hash(source) != source_hash:
            raise ValueError("LEGACY_SOURCE_CHANGED")
    except BaseException:
        target.delete(imported["id"], active_id=None, purge=True)
        raise
    return {"migrated": 1, "source_unchanged": True, "source_id": voice_id,
            "profile_id": imported["id"], "version": imported["version"]}
