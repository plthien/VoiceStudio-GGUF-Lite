from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]

def test_profiler_patch_is_pinned_bounded_and_sensitive_data_free():
    patch=(ROOT/"scripts/internal/instrument-native-performance.py").read_text()
    header=(ROOT/"scripts/internal/lite-native-perf.h").read_text()
    assert "886fc079838ca7400cb2b42b36e2a65aa1daabe8" in patch
    for bucket in ("Synth","Reference","Maskgit","CodecEncode","CodecDecode","Postprocess"):
        assert bucket in header
    assert "[MaskGIT-Step]" in patch and "suppressed" in patch
    for forbidden in ("ref_text","model_path","audio_24k"):
        assert forbidden not in header

def test_worker_preserves_result_and_adds_private_perf_only():
    source=(ROOT/"scripts/internal/lite-persistent-worker.cpp").read_text()
    assert "RESULT=103" in source and "GET_PERF=6" in source and "PERF=105" in source
    assert "LitePerf/v1" in source and "ov_synthesize_with_speed(ctx" in source
