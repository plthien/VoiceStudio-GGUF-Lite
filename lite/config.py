"""Pinned, offline-only, host-local GGUF Lite settings."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO_ID = "Serveurperso/OmniVoice-GGUF"
MODEL_REVISION = "361609388ae572a820d085185bbbe2a2aac4b30e"
RUNTIME_REVISION = "886fc079838ca7400cb2b42b36e2a65aa1daabe8"
BASE_NAME = "omnivoice-base-Q8_0.gguf"
TOKENIZER_NAME = "omnivoice-tokenizer-Q8_0.gguf"
NATIVE_NAME = "omnivoice-tts-windows-x86_64.exe"
VULKAN_NAME = "ggml-vulkan.dll"
DATA_DEFAULT = r"D:\VoiceStudioBuild\lite\data"
BIN_DEFAULT = r"D:\VoiceStudioBuild\lite\runtime\bin"
CACHE_DEFAULT = r"D:\VoiceStudioBuild\models"

@dataclass(frozen=True)
class Settings:
    data_dir: Path
    cache_dir: Path
    bin_dir: Path
    timeout: float
    max_output_bytes: int = 100 * 1024 * 1024
    max_ref_bytes: int = 24 * 1024 * 1024
    max_ref_seconds: float = 60.0

    @classmethod
    def load(cls) -> "Settings":
        timeout = float(os.environ.get("GGUF_LITE_TIMEOUT_S", "900"))
        if not 15 <= timeout <= 3600:
            raise ValueError("GGUF_LITE_TIMEOUT_S must be 15..3600")
        return cls(
            data_dir=Path(os.environ.get("GGUF_LITE_DATA_DIR", DATA_DEFAULT)).expanduser().resolve(),
            cache_dir=Path(os.environ.get("OMNIVOICE_CACHE_DIR", CACHE_DEFAULT)).expanduser().resolve(),
            bin_dir=Path(os.environ.get("GGUF_LITE_BIN_DIR", BIN_DEFAULT)).expanduser().resolve(),
            timeout=timeout,
        )

def require_owned_path(path: Path, root: Path) -> Path:
    """Reject symlinks outside our store, including parents."""
    p = path.resolve(strict=True)
    r = root.resolve(strict=True)
    if not p.is_relative_to(r) or not p.is_file():
        raise ValueError("Reference is outside Lite voice store")
    return p
