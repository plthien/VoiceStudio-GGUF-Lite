from pathlib import Path
import shutil,subprocess
ROOT=Path(__file__).resolve().parents[2]
def test_candidate_settings_are_strict_and_defaults_unchanged():
    h=(ROOT/"scripts/internal/lite-maskgit-experiments.h").read_text()
    worker=(ROOT/"scripts/internal/lite-persistent-worker.cpp").read_text()
    assert "{32,24,16,12,8}" in h and "return 32" in h
    assert "tp.mg_num_step=maskgit_steps" in worker and "ip.use_fa=flash_attention" in worker
    assert "GRAPH_REUSE_DEFERRED" in (ROOT/"docs/gguf-lite-performance.md").read_text()

def test_reference_cache_exact_single_slot_and_off_by_default():
    patch=(ROOT/"scripts/internal/instrument-ref-conditioning-cache.py").read_text()
    helper=(ROOT/"scripts/internal/lite-reference-cache.h").read_text()
    assert "std::memcmp" in helper and 'strcmp(value,"0")' in helper
    assert "ref_rms_for_postproc" in patch and "const RefEncoded &" in patch
    assert "lite_prepare_or_reuse_ref" in patch
    assert "source_pcm.assign" in helper and "cache_hits" in helper and "cache_misses" in helper


def test_reference_cache_uses_actual_compiled_helper(tmp_path):
    compiler=shutil.which("clang++") or shutil.which("g++")
    assert compiler,"CI must provide the C++17 compiler used by the native contract gate"
    exe=tmp_path/("reference-cache-check.exe" if __import__('os').name=="nt" else "reference-cache-check")
    subprocess.run([compiler,"-std=c++17","-O0","-I",str(ROOT/"scripts/internal"),
                    str(ROOT/"scripts/internal/native-performance-ci.cpp"),"-o",str(exe)],
                   check=True,timeout=60)
    subprocess.run([str(exe)],check=True,timeout=30)
