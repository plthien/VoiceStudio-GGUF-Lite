"""Server-only root and active legacy SQLite WAL gate."""
import sqlite3
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from lite.config import Settings
from lite.app import create_app

def test_migration_rejects_legacy_wal_before_preview(tmp_path,monkeypatch):
    root=tmp_path/"legacy";root.mkdir()
    (root/"voices").mkdir()
    db=root/"omnivoice.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE voice_profiles(id TEXT,name TEXT,ref_audio_path TEXT,ref_text TEXT,language TEXT,kind TEXT)")
    (root/"omnivoice.db-wal").write_bytes(b"active")
    monkeypatch.setenv("GGUF_LITE_LEGACY_ROOT",str(root))
    s=Settings(data_dir=tmp_path/"data",cache_dir=tmp_path/"cache",bin_dir=tmp_path/"bin",timeout=30)
    with TestClient(create_app(s)) as c:
        result=c.get("/lite/migration/preview")
        assert result.status_code==409
        assert "D:\\" not in result.text and str(root) not in result.text
