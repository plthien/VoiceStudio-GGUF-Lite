"""Provider-owned immutable versioned reference/conditioning records (never Chatterbox Store)."""
from __future__ import annotations
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from .vieneu_config import PROVIDER, MODEL_REVISION, SDK_REVISION, VieNeuError, digest

_ID = re.compile(r"^[a-f0-9]{32}$")
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
def _atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("xb") as fp:
            fp.write(data); fp.flush(); os.fsync(fp.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)

class VoiceLibrary:
    def __init__(self, root: Path):
        self.root = root / "data" / "voices"
        self.root.mkdir(parents=True, exist_ok=True)

    def _folder(self, voice_id: str) -> Path:
        if not _ID.fullmatch(voice_id):
            raise VieNeuError("PROFILE_NOT_FOUND")
        d = self.root / voice_id
        if d.is_symlink() or d.resolve() != (self.root.resolve() / voice_id):
            raise VieNeuError("REFERENCE_INVALID")
        return d

    def get(self, voice_id: str) -> dict:
        path = self._folder(voice_id) / "profile.json"
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
            if obj["voice_id"] != voice_id or obj["provider_id"] != PROVIDER:
                raise VieNeuError("PROFILE_INTEGRITY_FAILED")
            return obj
        except FileNotFoundError:
            raise VieNeuError("PROFILE_NOT_FOUND")
        except (ValueError, KeyError) as exc:
            raise VieNeuError("PROFILE_INTEGRITY_FAILED") from exc

    def list(self) -> list[dict]:
        output = []
        for d in sorted(self.root.iterdir()):
            if d.is_dir() and not d.is_symlink() and _ID.fullmatch(d.name):
                try:
                    output.append(self.get(d.name))
                except VieNeuError:
                    continue
        return output

    def verify(self, voice_id: str) -> dict:
        obj = self.get(voice_id)
        if (obj["model_revision"] != MODEL_REVISION or obj["conditioning_revision"] != SDK_REVISION
            or obj["consent"] != "confirmed" or obj["language"] != "vi"):
            raise VieNeuError("PROFILE_INTEGRITY_FAILED")
        folder = self._folder(voice_id)
        reference = folder / ("reference-v%d.wav" % obj["voice_version"])
        conditioning = folder / ("conditioning-v%d.json" % obj["voice_version"])
        for p in (reference, conditioning):
            if p.is_symlink() or not p.is_file() or p.resolve().parent != folder.resolve():
                raise VieNeuError("PROFILE_INTEGRITY_FAILED")
        if digest(reference) != obj["reference_sha256"] or digest(conditioning) != obj["conditioning_sha256"]:
            raise VieNeuError("PROFILE_INTEGRITY_FAILED")
        try:
            data = json.loads(conditioning.read_text(encoding="utf-8"))
            if len(data["speaker_emb"]) != 192 or len(data["style"]) != 50:
                raise ValueError()
            if any(len(row) != 256 for row in data["style"]):
                raise ValueError()
        except (ValueError, TypeError, KeyError) as exc:
            raise VieNeuError("PROFILE_INTEGRITY_FAILED") from exc
        return {"profile": obj, "reference": reference, "conditioning": conditioning}

    def save(self, name: str, ref_bytes: bytes, conditioning_bytes: bytes,
             voice_id: str | None = None) -> dict:
        if not isinstance(name, str) or not name.strip() or len(name) > 128:
            raise VieNeuError("INVALID_REQUEST")
        is_new = voice_id is None
        old = None if is_new else self.verify(voice_id)["profile"]
        vid = uuid.uuid4().hex if is_new else voice_id
        folder = self._folder(vid)
        if is_new:
            folder.mkdir(mode=0o700)
        version = 1 if is_new else old["voice_version"] + 1
        ref = folder / ("reference-v%d.wav" % version)
        cond = folder / ("conditioning-v%d.json" % version)
        _atomic(ref, ref_bytes); _atomic(cond, conditioning_bytes)
        now = _now()
        meta = {"voice_id": vid, "id": vid, "provider_id": PROVIDER, "language": "vi",
                "name": name.strip(), "voice_type": "cloned", "voice_version": version,
                "version": version, "model_revision": MODEL_REVISION, "conditioning_revision": SDK_REVISION,
                "reference_sha256": digest(ref), "conditioning_sha256": digest(cond),
                "consent": "confirmed", "created_at": now if is_new else old["created_at"],
                "updated_at": now}
        _atomic(folder / "profile.json", json.dumps(meta, sort_keys=True, ensure_ascii=False).encode("utf-8"))
        return meta

    def rename(self, voice_id: str, name: str) -> dict:
        if not isinstance(name, str) or not name.strip() or len(name) > 128:
            raise VieNeuError("INVALID_REQUEST")
        meta = self.verify(voice_id)["profile"]
        meta["name"] = name.strip()
        meta["updated_at"] = _now()
        _atomic(self._folder(voice_id) / "profile.json", json.dumps(meta, ensure_ascii=False).encode("utf-8"))
        return meta

    def delete(self, voice_id: str) -> None:
        import shutil
        self.verify(voice_id)
        folder = self._folder(voice_id)
        # refuse any linked paths before recursive deletion
        if any(p.is_symlink() for p in folder.rglob("*")):
            raise VieNeuError("REFERENCE_INVALID")
        shutil.rmtree(folder)
