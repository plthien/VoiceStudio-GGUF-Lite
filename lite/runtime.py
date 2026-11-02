"""Offline Q8 identity, checked native binaries and verified NVIDIA Vulkan0."""
from __future__ import annotations
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from .config import (ROOT, REPO_ID, MODEL_REVISION, RUNTIME_REVISION,
                     BASE_NAME, TOKENIZER_NAME, NATIVE_NAME, VULKAN_NAME, Settings)

_HEX = re.compile(r"^[0-9a-f]{64}$")
_GPU = re.compile(r"^GPU([0-9]+):$", re.M)
_NAME = re.compile(r"^deviceName\s*=\s*(.+)$", re.M)
_VENDOR = re.compile(r"^vendorID\s*=\s*(\S+)", re.M)
_KIND = re.compile(r"^deviceType\s*=\s*(\S+)", re.M)

class RuntimeErrorLite(RuntimeError):
    pass

def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1048576), b""):
            h.update(block)
    return h.hexdigest()

def _manifest(bin_dir: Path) -> dict[str, str]:
    path = bin_dir / "checksums.sha256"
    if not path.is_file():
        raise RuntimeErrorLite("Native checksums.sha256 is required")
    records = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split(None, 1)
        if len(fields) != 2:
            raise RuntimeErrorLite("Invalid native checksum manifest")
        digest, name = fields[0].lower(), fields[1].lstrip("* ").strip()
        if not _HEX.fullmatch(digest) or Path(name).name != name or name in records:
            raise RuntimeErrorLite("Invalid native checksum manifest")
        records[name] = digest
    return records

def native_files(settings: Settings) -> tuple[Path, Path]:
    exe, dll = settings.bin_dir / NATIVE_NAME, settings.bin_dir / VULKAN_NAME
    root = settings.bin_dir.resolve(strict=True)
    for candidate in (exe, dll):
        if not candidate.resolve().is_relative_to(root):
            raise RuntimeErrorLite("Native binary symlink escapes bin directory")
    checksums = _manifest(settings.bin_dir)
    for p in (exe, dll):
        if p.name not in checksums or not p.is_file() or p.stat().st_size < 64:
            raise RuntimeErrorLite("Required native executable or Vulkan DLL is missing/untrusted")
        if sha256(p) != checksums[p.name]:
            raise RuntimeErrorLite("Native binary checksum mismatch")
    if exe.open("rb").read(2) != b"MZ":
        raise RuntimeErrorLite("Native executable is not a Windows PE file")
    if dll.open("rb").read(2) != b"MZ":
        raise RuntimeErrorLite("Vulkan library is not a Windows PE DLL")
    # Shared GGML dependencies are part of the executable trust boundary.
    for lib in settings.bin_dir.iterdir():
        if lib.is_file() and lib.name.lower().startswith("ggml") and lib.suffix.lower() == ".dll":
            if lib.name not in checksums or sha256(lib) != checksums[lib.name]:
                raise RuntimeErrorLite("Untrusted GGML runtime dependency")
    return exe, dll

def _expected_quantmap() -> dict:
    pins=json.loads((ROOT/"lite/resources/q8_pins.json").read_text(encoding="utf-8"))
    if (pins.get("schema_version")!=2 or pins.get("model_revision")!=MODEL_REVISION or
        pins.get("runtime_revision")!=RUNTIME_REVISION or pins.get("quant")!="Q8_0" or
        pins.get("base",{}).get("name")!=BASE_NAME or pins.get("tokenizer",{}).get("name")!=TOKENIZER_NAME or
        not all(_HEX.fullmatch(pins[k].get("sha256","")) for k in ("base","tokenizer"))):
        raise RuntimeErrorLite("Pinned Q8_0 metadata mismatch")
    return pins

def model_pair(settings: Settings) -> tuple[Path, Path]:
    pins=_expected_quantmap()
    repo_cache = settings.cache_dir / "models--Serveurperso--OmniVoice-GGUF" / "snapshots" / MODEL_REVISION
    # Explicit direct install or standard Hugging Face revision-pinned snapshot.
    candidates = [repo_cache, settings.cache_dir / MODEL_REVISION]
    for base_dir in candidates:
        a, b = base_dir / BASE_NAME, base_dir / TOKENIZER_NAME
        if a.is_file() and b.is_file():
            # HF snapshots contain relative symlinks into cache/blobs.
            if not all(p.resolve().is_relative_to(settings.cache_dir.resolve()) for p in (a, b)):
                raise RuntimeErrorLite("Model cache symlink escapes configured cache")
            for p,key in ((a,"base"),(b,"tokenizer")):
                if p.stat().st_size<64:
                    raise RuntimeErrorLite("Truncated pinned Q8 GGUF")
                with p.open("rb") as fd: header=fd.read(24)
                if (header[:4]!=b"GGUF" or int.from_bytes(header[4:8],"little") not in (2,3)
                    or not 0<int.from_bytes(header[8:16],"little")<1000000):
                    raise RuntimeErrorLite("Invalid GGUF header")
                if sha256(p)!=pins[key]["sha256"]:
                    raise RuntimeErrorLite("Q8 trusted SHA256 mismatch")
            return a.resolve(), b.resolve()
    raise RuntimeErrorLite("Offline pinned Q8_0 base/tokenizer missing; preinstall both at exact revision")

def parse_vulkan_devices(output: str) -> list[dict]:
    """GGML-like indexing: GPU sections, discrete/integrated only, no ambiguity."""
    sections = _GPU.split(output)
    devices = []
    for i in range(1, len(sections), 2):
        section = sections[i + 1]
        name, vendor, kind = (_NAME.search(section), _VENDOR.search(section), _KIND.search(section))
        if not (name and vendor and kind) or not any(s in kind.group(1) for s in ("DISCRETE_GPU", "INTEGRATED_GPU")):
            continue
        devices.append({"index": len(devices), "vk_index": int(sections[i]),
                        "name": name.group(1).strip(),
                        "vendor_id": vendor.group(1).lower(),
                        "type": kind.group(1)})
    return devices

def nvidia_device(*, run=subprocess.run, which=shutil.which) -> dict:
    path = which("vulkaninfo")
    if not path:
        raise RuntimeErrorLite("vulkaninfo --summary unavailable")
    try:
        result = run([path, "--summary"], capture_output=True, text=True,
                                errors="replace", timeout=65, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeErrorLite("vulkaninfo probe failed") from exc
    if result.returncode != 0:
        raise RuntimeErrorLite("vulkaninfo probe failed")
    devices = parse_vulkan_devices(result.stdout)
    # Fail closed on any unidentifiable earlier Vulkan GPU: index 0 cannot be trusted.
    if not devices or devices[0]["vk_index"] != 0:
        raise RuntimeErrorLite("Vulkan0 is not unambiguously identified")
    gpu = devices[0]
    if (gpu["vendor_id"] not in ("0x10de", "4318") or
        "GTX 1650" not in gpu["name"].upper() or
        "DISCRETE_GPU" not in gpu["type"]):
        raise RuntimeErrorLite("Vulkan0 is not the verified NVIDIA GTX 1650")
    # Fail when no independent VRAM evidence is available, rather than inventing 4 GB.
    smi = which("nvidia-smi")
    if not smi:
        raise RuntimeErrorLite("nvidia-smi required for GTX 1650 VRAM verification")
    try:
        probe = run([smi, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                               capture_output=True, text=True, errors="replace", timeout=15, check=False)
        rows = [line.strip() for line in probe.stdout.splitlines() if line.strip()]
        same = [x for x in rows if "GTX 1650" in x.upper()]
        if probe.returncode or len(same) != 1 or len(rows) != 1:
            raise RuntimeErrorLite("Ambiguous NVIDIA GPU inventory")
        vram = int(same[0].rsplit(",", 1)[1].strip())
        if not 3500 <= vram <= 4500:
            raise RuntimeErrorLite("GTX 1650 VRAM is not approximately 4 GB")
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError) as exc:
        raise RuntimeErrorLite("NVIDIA VRAM verification failed") from exc
    return {**gpu, "vram_mib": vram, "backend": "Vulkan0", "verified": True}

def preflight(settings: Settings) -> dict:
    result = {"ready": False, "quant": {"selected": BASE_NAME, "installed": False, "verified": False,
                                      "prepared_quant": None, "in_use": None,
                                      "base": BASE_NAME, "tokenizer": TOKENIZER_NAME,
                                      "revision": MODEL_REVISION, "runtime_revision": RUNTIME_REVISION},
              "gpu": None, "binary": False, "model_pair": False, "issues": []}
    try:
        native_files(settings)
        result["binary"] = True
    except (OSError, ValueError, RuntimeErrorLite) as exc:
        result["issues"].append(str(exc))
    try:
        model_pair(settings)
        result["model_pair"] = True
        result["quant"].update(installed=True,verified=True,prepared_quant="Q8_0")
    except (OSError, ValueError, RuntimeErrorLite) as exc:
        result["issues"].append(str(exc))
    try:
        result["gpu"] = nvidia_device()
    except (OSError, ValueError, RuntimeErrorLite) as exc:
        result["issues"].append(str(exc))
    result["ready"] = bool(result["binary"] and result["model_pair"] and result["gpu"])
    # Preflight cannot assert in_use absent successful native inference.
    return result

def native_env(settings: Settings) -> dict[str, str]:
    env = os.environ.copy()
    # Disable overrides that can silently select CPU/AMD; actual binary selection is
    # additionally checked by the Vulkan0 preflight above.
    env["GGML_BACKEND"] = "Vulkan0"
    env["OMNIVOICE_GGUF_BACKEND"] = "vulkan"
    env["OMNIVOICE_GGUF_DEVICE"] = "0"
    env["PATH"] = str(settings.bin_dir) + os.pathsep + env.get("PATH", "")
    return env
