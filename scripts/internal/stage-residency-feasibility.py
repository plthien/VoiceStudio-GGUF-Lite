"""Opt-in pinned-source staging of a REAL same-ov_context A-B-A feasibility probe.

Never patch the upstream/frozen Git branch, installed runtime, or model.
Called only after LiteAttest and LiteNativeSpeed exact source patches.
"""
from pathlib import Path
import subprocess
import sys

PIN="886fc079838ca7400cb2b42b36e2a65aa1daabe8"
CMAKE_ANCHOR = "add_executable(omnivoice-tts tools/omnivoice-tts.cpp)\ntarget_link_libraries(omnivoice-tts PRIVATE omnivoice-core)\nlink_ggml_backends(omnivoice-tts)\n"
CMAKE_ADD = """# Optional *feasibility* tool. Never part of the shipped native TTS service.
add_executable(lite-reuse-probe tools/lite-reuse-probe.cpp)
target_link_libraries(lite-reuse-probe PRIVATE omnivoice-core)
link_ggml_backends(lite-reuse-probe)
"""
def stage(staging:Path):
    staging=staging.resolve(strict=True)
    project=Path(__file__).resolve().parents[2]
    rev=subprocess.run(["git","-C",str(staging),"rev-parse","HEAD"],capture_output=True,text=True,check=True,timeout=15).stdout.strip()
    if rev!=PIN: raise ValueError("Pinned C++ revision mismatch")
    header=(staging/"src/omnivoice.h").read_text(encoding="utf8")
    source=(staging/"src/omnivoice.cpp").read_text(encoding="utf8")
    cli=(staging/"tools/omnivoice-tts.cpp").read_text(encoding="utf8")
    backend=(staging/"src/backend.h").read_text(encoding="utf8")
    if ("OV_ABI_VERSION 2" not in header or
        "ov_synthesize_with_speed(" not in header or
        "ov_synthesize_with_speed(" not in source or
        "LiteNativeSpeed/v1" not in cli or "[LiteAttest/v1]" not in backend):
        raise ValueError("Prior attestation/native-speed anchored patches missing")
    cmake=staging/"CMakeLists.txt"
    old=cmake.read_text(encoding="utf8")
    destination=staging/"tools/lite-reuse-probe.cpp"
    if old.count(CMAKE_ANCHOR)!=1 or CMAKE_ADD in old or destination.exists():
        raise ValueError("Source drift or already-staged feasibility probe")
    probe=(project/"scripts/internal/native-residency-feasibility.cpp").read_bytes()
    if not probe or b"ov_synthesize_with_speed" not in probe:
        raise ValueError("Missing real context reuse harness")
    cmake.write_text(old.replace(CMAKE_ANCHOR,CMAKE_ANCHOR+"\n"+CMAKE_ADD),encoding="utf8")
    destination.write_bytes(probe)
    print("LITE_REUSE_PROBE_STAGED",PIN)
if __name__=="__main__":
    if len(sys.argv)!=2: raise SystemExit("Usage: stage-residency-feasibility.py <staged pinned source>")
    stage(Path(sys.argv[1]))
