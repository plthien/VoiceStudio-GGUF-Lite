"""Trusted, fixed-root legacy import wrapper, with no browser-supplied paths."""
from __future__ import annotations
import os
import re
from pathlib import Path
from .migrate import _rows,_validate,migrate

def legacy_sources() -> tuple[Path,Path]:
    # Custom roots are configured by the owner OUTSIDE the browser.
    appdata=Path(os.environ.get("APPDATA","")).expanduser() if os.environ.get("APPDATA") else None
    default=appdata/"OmniVoice" if appdata is not None else None
    root=Path(os.environ["GGUF_LITE_LEGACY_ROOT"]).expanduser() if os.environ.get("GGUF_LITE_LEGACY_ROOT") else default
    if root is None:
        raise ValueError("Configure GGUF_LITE_LEGACY_ROOT in the server environment")
    if root.is_symlink():
        raise ValueError("Legacy root cannot be a symlink")
    root=root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Legacy root unavailable")
    database=Path(os.environ["GGUF_LITE_LEGACY_DB"]) if os.environ.get("GGUF_LITE_LEGACY_DB") else root/"voices.sqlite3"
    voices=Path(os.environ["GGUF_LITE_LEGACY_VOICES"]) if os.environ.get("GGUF_LITE_LEGACY_VOICES") else root/"voices"
    for source in (database,voices):
        if source.is_symlink() or not source.resolve(strict=True).is_relative_to(root):
            raise ValueError("Legacy path outside server-configured root")
    if not database.is_file() or not voices.is_dir():
        raise ValueError("Legacy DB/voice directory absent")
    return database.resolve(), voices.resolve()

def migration_preview(db: Path, voices:Path) -> dict:
    rows=_rows(db)
    accepted,invalid=_validate(rows,voices,None)
    result={"configured":True,"eligible":[],"invalid":[]}
    for row,_,_,_ in accepted:
        result["eligible"].append({"id":row["id"],"name":row["name"],
                                   "language":row.get("language") or "en","state":"eligible"})
    for item in invalid:
        result["invalid"].append({"id":item.get("id"),"reason":item["reason"]})
    return result

def migration_apply(db:Path,voices:Path,settings,voice_id:str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}",voice_id):
        raise ValueError("Invalid stable ID")
    return migrate(db,voices,settings=settings,only_id=voice_id,apply=True)
