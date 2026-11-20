"""Online-only installer download and offline verification helpers."""
from __future__ import annotations
import argparse
import importlib.metadata as metadata
import json
import os
import shutil
from pathlib import Path

from .vieneu_config import (MODEL_REPO, MODEL_REVISION, SDK_VERSION, SDK_REVISION,
                            MODEL_HASHES, model_dir, root_dir, digest, verify_model,
                            VieNeuError)

def assert_sdk():
    try:
        package = metadata.distribution("vieneu")
        if package.version != SDK_VERSION:
            raise VieNeuError("SDK_VERSION_MISMATCH")
        direct = json.loads(package.read_text("direct_url.json") or "{}")
        if direct.get("vcs_info", {}).get("commit_id") != SDK_REVISION:
            raise VieNeuError("SDK_SOURCE_MISMATCH")
    except metadata.PackageNotFoundError as exc:
        raise VieNeuError("SDK_NOT_INSTALLED") from exc
    for forbidden in ("torch", "torchaudio"):
        try:
            metadata.distribution(forbidden)
        except metadata.PackageNotFoundError:
            continue
        raise VieNeuError("TORCH_NOT_ALLOWED")
    # Check modules required by cloning without triggering model load.
    import onnxruntime
    if "CPUExecutionProvider" not in onnxruntime.get_available_providers():
        raise VieNeuError("CPU_PROVIDER_UNAVAILABLE")

def install_snapshot():
    from huggingface_hub import snapshot_download
    root = root_dir()
    folder = model_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    files = [*MODEL_HASHES, "config.json"]
    snapshot = Path(snapshot_download(repo_id=MODEL_REPO, revision=MODEL_REVISION,
                   allow_patterns=files, local_files_only=False))
    for name in files:
        src = snapshot / name
        if not src.is_file():
            raise VieNeuError("MODEL_FILES_MISSING")
        if name in MODEL_HASHES and digest(src) != MODEL_HASHES[name]:
            raise VieNeuError("MODEL_HASH_MISMATCH")
        target = folder / name
        stage = target.with_name(target.name + ".download")
        try:
            shutil.copyfile(src, stage)
            os.replace(stage, target)
        finally:
            stage.unlink(missing_ok=True)
    hashes = {name: digest(folder / name) for name in files}
    data = {"model_repo": MODEL_REPO, "model_revision": MODEL_REVISION,
            "sdk_version": SDK_VERSION, "sdk_revision": SDK_REVISION,
            "files": hashes}
    stage = folder / "manifest.json.partial"
    try:
        stage.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(stage, folder / "manifest.json")
    finally:
        stage.unlink(missing_ok=True)
    verify_model(root)

def inventory():
    """Capture installed distribution and declared license metadata for owner audit."""
    records = []
    for package in sorted(metadata.distributions(), key=lambda d: d.metadata.get("Name", "").lower()):
        records.append({"package": package.metadata.get("Name", "unknown"),
                        "version": package.version,
                        "license_expression": package.metadata.get("License-Expression"),
                        "license": package.metadata.get("License"),
                        "classifiers": [v for v in package.metadata.get_all("Classifier", [])
                                        if v.startswith("License ::")]})
    target = root_dir() / "private-dependency-license-inventory.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(target)

def smoke():
    # Strictly local: model snapshot + all cloning graphs must already exist.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
    from vieneu import Vieneu
    model = Vieneu(mode="v3nano", onnx_dir=str(model_dir()), threads=4)
    if model.backend != "onnx" or model.sample_rate != 24000:
        raise VieNeuError("CPU_PROVIDER_UNAVAILABLE")
    presets = model.list_preset_voices()
    if not presets:
        raise VieNeuError("PRESET_VOICES_UNAVAILABLE")
    model.engine._ensure_clone_sessions()
    print("LOCAL_MODEL_AND_CLONING_GRAPHS_LOADED; presets=" + str(len(presets)))
    try:
        # Loading graphs alone misses G2P/ONNX defects during actual synthesis.
        import numpy as np
        audio = np.asarray(model.infer("Xin chào Việt Nam.", voice=presets[0][1],
                         steps=16, cfg=3.0, max_chars=140, apply_watermark=False))
        if audio.ndim != 1 or audio.size < 2400 or not np.all(np.isfinite(audio)):
            raise VieNeuError("VIENEU_INFERENCE_SMOKE_FAILED")
        print("LOCAL_VIETNAMESE_INFERENCE_OK; frames=" + str(audio.size))
    finally:
        model.close()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("download", "verify", "check-sdk"))
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    assert_sdk()
    if args.action == "download":
        install_snapshot()
    elif args.action == "verify":
        verify_model()
    if args.smoke and args.action != "check-sdk":
        smoke()
    if args.action == "download":
        inventory()
    print("VIENEU_V3NANO_" + args.action.upper() + "_OK")
if __name__ == "__main__":
    main()
