"""Verified speed-capability receipt for a pinned, checksum-validated Windows native binary.

No CLI help marker alone is sufficient. No model weight scan or GPU probe.
Old installed binaries remain fully supported at speed=1.0.
"""
import hashlib
import json
import subprocess
import math
import struct
from .config import ROOT, RUNTIME_REVISION, Settings
from .runtime import native_files, native_env

def speed_to_native(requested: float) -> tuple[float,str]:
    """9 significant digits round-trip the finite IEEE754 float32 passed to strtof.

    Retains arbitrary accepted JSON float precision in Requested header; the
    Effective header identifies the real C float32 argument.
    """
    if not isinstance(requested,(float,int)) or not math.isfinite(requested) or not 0.75 <= requested <= 1.25:
        raise ValueError("Invalid requested native speed")
    effective=struct.unpack("<f",struct.pack("<f",requested))[0]
    if not math.isfinite(effective) or not 0.75 <= effective <= 1.25:
        raise ValueError("Invalid float32 native speed")
    return effective, format(effective,".9g")

MARKER="LiteNativeSpeed/v1"
SCHEMA="lite-native-speed-v1"

def digest(path):
    h=hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda:f.read(1024*1024),b""):
            h.update(block)
    return h.hexdigest()

def native_speed_capability(settings:Settings)->dict:
    result={"supported":False,"marker":MARKER,"min":0.75,"max":1.25,
            "reason":"Native Speed requires rebuilt Lite Vulkan runtime"}
    try:
        exe,_=native_files(settings)  # Manifest + all GGML DLL SHA checks.
        receipt_path=settings.bin_dir/"speed-capability.json"
        if receipt_path.is_symlink() or not receipt_path.is_file() or receipt_path.stat().st_size>4096:
            return result
        receipt=json.loads(receipt_path.read_text(encoding="utf8"))
        if (receipt.get("schema")!=SCHEMA or
            receipt.get("runtime_revision")!=RUNTIME_REVISION or
            receipt.get("exe_sha256")!=digest(exe) or
            receipt.get("patch_sha256")!=digest(ROOT/"scripts/internal/instrument-native-speed.py") or
            receipt.get("header_sha256")!=digest(ROOT/"scripts/internal/lite-native-speed.h")):
            return result
        proc=subprocess.run([str(exe),"--help"],env=native_env(settings),
                            capture_output=True,timeout=8,check=False)
        output=(proc.stdout+proc.stderr)[:32768]
        if proc.returncode!=0 or MARKER.encode("ascii") not in output or b"--speed" not in output:
            return result
    except (OSError,ValueError,subprocess.SubprocessError,KeyError,TypeError):
        return result
    except Exception:
        # Fails closed for a missing bin, malformed manifest, or untrusted DLL.
        return result
    return {**result,"supported":True,"reason":""}
