"""Source transformation test executes real patch helper on a pinned, audited C++ fragment."""
from pathlib import Path
import subprocess
import sys
import pytest

ROOT=Path(__file__).resolve().parents[2]
SCRIPT=ROOT/"scripts/internal/instrument-attestation.py"
ANCHOR='    ov_log(OV_LOG_INFO, "[Load] %s backend: %s (CPU threads: %d)", label, ggml_backend_name(bp.backend), n_threads);'

def test_exact_cxx_anchor_and_fail_closed(tmp_path):
    header=tmp_path/"backend.h"
    header.write_text("static BackendPair backend_init(const char* label) {\n"+ANCHOR+"\n}\n")
    done=subprocess.run([sys.executable,str(SCRIPT),str(header)],capture_output=True,text=True)
    assert done.returncode==0,done.stderr
    patched=header.read_text()
    assert 'ggml_backend_get_device(bp.backend)' in patched
    assert 'ggml_backend_dev_description(lite_dev)' in patched
    assert '[LiteAttest/v1] backend=%s; device=%s; description=%s' in patched
    repeat=subprocess.run([sys.executable,str(SCRIPT),str(header)],capture_output=True,text=True)
    assert repeat.returncode!=0
    header.write_text("backend_init() {}")
    bad=subprocess.run([sys.executable,str(SCRIPT),str(header)],capture_output=True,text=True)
    assert bad.returncode!=0 and header.read_text()=="backend_init() {}"

def test_lite_build_references_complete_source_closure():
    ps=(ROOT/"scripts/build-gguf-lite-vulkan.ps1").read_text()
    sh=(ROOT/"scripts/internal/build-gguf-lite.sh").read_text()
    assert 'instrument-attestation.py' in ps and 'instrument-attestation.py' in sh
    assert '886fc079838ca7400cb2b42b36e2a65aa1daabe8' in ps
    assert 'ggml-vulkan.dll' in sh
    assert 'CPU fallback forbidden' in sh
    assert 'GGML_VULKAN=ON' not in sh or 'try_vulkan_build' in sh
    assert "backend/" not in sh
