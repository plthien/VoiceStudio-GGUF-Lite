"""Opt-in CPU-only pinned Turbo diagnostic; NOT a production fallback."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import time
from pathlib import Path

from .audio import wav_info
from .chatterbox_assembly import atomic
from .chatterbox_config import ChatterboxSettings, MODEL_REVISION, verify_model_files
from .profiles import Store


def cpu_diagnostic(voice: str, text_file: Path, output: Path):
    settings = ChatterboxSettings.load()
    if os.name == "nt" and output.resolve().drive.upper() != "D:":
        raise ValueError("D_DRIVE_ONLY")
    raw = text_file.read_text(encoding="utf-8")
    if not 1 <= len(raw) <= 4096 or not raw.strip():
        raise ValueError("INVALID_INPUT")
    store = Store(settings)
    profile = store.get(voice)
    if not profile or profile["consent"] != "confirmed" or profile["language"] != "en":
        raise ValueError("CONSENTED_ENGLISH_PROFILE_REQUIRED")
    reference = store.reference(profile)
    if reference.stat().st_size > settings.max_ref_bytes:
        raise ValueError("REFERENCE_TOO_LARGE")
    verified, issues = verify_model_files(settings)
    if not verified:
        raise ValueError(issues[0])
    # This explicit CLI does not start port 3901 or any CUDA worker.
    from .chatterbox_worker import pcm_wav
    import torch
    from chatterbox.tts_turbo import ChatterboxTurboTTS
    started = time.monotonic()
    with torch.inference_mode():
        with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink):
            model = ChatterboxTurboTTS.from_local(str(settings.model_dir), device="cpu")
            model.prepare_conditionals(str(reference))
            generated = model.generate(raw)
    wave = pcm_wav(generated)
    info = wav_info(wave, max_bytes=settings.max_output_bytes, max_seconds=600)
    elapsed = time.monotonic() - started
    atomic(output, wave)
    metrics = {"schema": "chatterbox-cpu-diagnostic-v1", "backend": "CPU_DIAGNOSTIC_ONLY",
               "production_go": False, "gpu_rtf": None, "model_revision": MODEL_REVISION,
               "elapsed_s": elapsed, "audio_s": info["seconds"], "cpu_rtf": elapsed / info["seconds"],
               "sha256": info["sha256"]}
    atomic(output.with_suffix(".metrics.json"), json.dumps(metrics, indent=2).encode())
    print(json.dumps({k:v for k,v in metrics.items() if k != "sha256"}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Optional CPU diagnosis; never a CUDA success")
    parser.add_argument("--voice-id", required=True)
    parser.add_argument("--text-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cpu_diagnostic(args.voice_id, args.text_file, args.output)
