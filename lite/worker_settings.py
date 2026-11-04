"""Versioned, atomic settings for the optional resident native worker."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

ALLOWED_IDLE_TIMEOUTS = (60, 300, 600, 1800, 3600, None)
ALLOWED_MODES = ("legacy_oneshot", "persistent")
SCHEMA = "lite-worker-settings-v1"


class WorkerSettingsError(ValueError):
    pass


@dataclass(frozen=True)
class WorkerSettings:
    mode: str = "legacy_oneshot"
    idle_timeout_seconds: int | None = 600

    def validate(self) -> "WorkerSettings":
        if self.mode not in ALLOWED_MODES:
            raise WorkerSettingsError("mode must be legacy_oneshot or persistent")
        if self.idle_timeout_seconds not in ALLOWED_IDLE_TIMEOUTS:
            raise WorkerSettingsError("idle timeout must be 60, 300, 600, 1800, 3600, or null")
        return self

    def as_dict(self) -> dict:
        return {"schema": SCHEMA, "mode": self.mode,
                "idle_timeout_seconds": self.idle_timeout_seconds}


class WorkerSettingsStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.path = data_dir / "worker-settings.json"

    def load(self) -> WorkerSettings:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return WorkerSettings()
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise WorkerSettingsError("saved worker settings are invalid") from exc
        if raw.get("schema") != SCHEMA:
            raise WorkerSettingsError("unsupported worker settings schema")
        return WorkerSettings(raw.get("mode"), raw.get("idle_timeout_seconds")).validate()

    def save(self, value: WorkerSettings) -> WorkerSettings:
        value.validate()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(value.as_dict(), sort_keys=True, separators=(",", ":")) + "\n").encode()
        fd, name = tempfile.mkstemp(prefix=".worker-settings-", suffix=".tmp", dir=self.data_dir)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass
        return value
