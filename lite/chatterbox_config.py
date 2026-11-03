"""Isolated Turbo settings. Never load GGUF/Vulkan configuration or licences."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

SOURCE_REVISION = "5de7a54aa4e5e2baadb0182dde554908b48b85c2"
MODEL_REVISION = "74971fa28ff1cdf9a2cf4b41d102cb31fd32d411"
MODEL_REPO = "ResembleAI/chatterbox-turbo"
MODEL_HASHES = {
    "t3_turbo_v1.safetensors": "fcf1f8c1d651bb7e3acd69ee5be269b4ac10c02980b7708213d598bc9f7cdf87",
    "s3gen_meanflow.safetensors": "d65cb687a2ed581ee6cc297e919ffefa63386944f42364ae13b78a594945514f",
}
REQUIRED_FILES = ("t3_turbo_v1.safetensors", "s3gen_meanflow.safetensors", "ve.safetensors",
                  "added_tokens.json", "merges.txt", "vocab.json", "special_tokens_map.json",
                  "tokenizer_config.json")
IDLE_CHOICES = (60, 300, 600, 1800, 3600, None)
DEFAULT_ROOT = r"D:\VoiceStudioBuild\chatterbox-turbo"


@dataclass(frozen=True)
class ChatterboxSettings:
    root: Path
    timeout: float = 900.0
    port: int = 3901
    max_ref_bytes: int = 24 * 1024 * 1024
    max_ref_seconds: float = 60
    max_output_bytes: int = 100 * 1024 * 1024

    @classmethod
    def load(cls):
        root = Path(os.environ.get("CHATTERBOX_ROOT", DEFAULT_ROOT)).expanduser().resolve()
        timeout = float(os.environ.get("CHATTERBOX_TIMEOUT_S", "900"))
        port = int(os.environ.get("CHATTERBOX_PORT", "3901"))
        if not 15 <= timeout <= 3600 or not 1 <= port <= 65535:
            raise ValueError("Invalid Chatterbox timeout/port")
        return cls(root=root, timeout=timeout, port=port)

    @property
    def data_dir(self):
        return self.root / "data"

    @property
    def cache_dir(self):
        return self.root / "cache"

    @property
    def model_dir(self):
        return self.root / "models" / MODEL_REVISION

    @property
    def venv_python(self):
        return self.root / ".venv-chatterbox" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    @property
    def bin_dir(self):
        return self.root / "bin"


def verify_model_files(settings: ChatterboxSettings) -> tuple[bool, list[str]]:
    """Verify the entire local snapshot against a private installer manifest."""
    import hashlib
    manifest = settings.model_dir / "manifest.json"
    try:
        obj = json.loads(manifest.read_text(encoding="utf-8"))
        if obj.get("revision") != MODEL_REVISION or obj.get("source_revision") != SOURCE_REVISION:
            return False, ["MODEL_REVISION_MISMATCH"]
        digests = obj["sha256"]
        if not set(REQUIRED_FILES).issubset(set(digests)):
            return False, ["MODEL_FILES_MISSING"]
        for name in REQUIRED_FILES:
            p = settings.model_dir / name
            if not p.is_file() or p.is_symlink() or p.resolve().parent != settings.model_dir.resolve():
                return False, ["MODEL_FILES_MISSING"]
            h = hashlib.sha256()
            with p.open("rb") as f:
                for part in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(part)
            if h.hexdigest() != digests[name] or (name in MODEL_HASHES and h.hexdigest() != MODEL_HASHES[name]):
                return False, ["MODEL_HASH_MISMATCH"]
        return True, []
    except (OSError, ValueError, KeyError, TypeError):
        return False, ["MODEL_MANIFEST_UNAVAILABLE"]


def diagnostic(settings: ChatterboxSettings) -> dict:
    import shutil
    installed = settings.venv_python.is_file()
    manifest = settings.model_dir / "manifest.json"
    # Cheap readiness: full SHA validation occurs at worker LOAD and CLI --verify.
    recorded = False
    try:
        j = json.loads(manifest.read_text(encoding="utf-8"))
        recorded = (j["revision"] == MODEL_REVISION and j["source_revision"] == SOURCE_REVISION
                    and set(REQUIRED_FILES).issubset(j["sha256"]))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    issues = ([] if installed else ["ENV_NOT_INSTALLED"]) + ([] if recorded else ["MODEL_MANIFEST_UNAVAILABLE"])
    if not shutil.which("ffmpeg"):
        issues.append("FFMPEG_UNAVAILABLE")
    return {"installed": installed, "model_manifest_recorded": recorded, "model_hash_verified": False,
            "hash_check": "on worker load or --verify", "ready": not issues,
            "cuda": "OWNER TEST PENDING", "model_revision": MODEL_REVISION,
            "source_revision": SOURCE_REVISION, "issues": issues}


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    settings = ChatterboxSettings.load()
    report = diagnostic(settings)
    if args.verify:
        ok, issues = verify_model_files(settings)
        report.update(model_hash_verified=ok, issues=sorted(set(report["issues"] + issues)),
                      ready=report["ready"] and ok)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
