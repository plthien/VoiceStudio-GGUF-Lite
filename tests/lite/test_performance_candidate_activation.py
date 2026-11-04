from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
def test_candidate_build_never_targets_installed_runtime_or_auto_switches():
    build=(ROOT/"scripts/build-gguf-lite-vulkan.ps1").read_text()
    switch=(ROOT/"scripts/switch-gguf-lite-runtime.ps1").read_text()
    verify=(ROOT/"scripts/verify-performance-candidate.ps1").read_text()
    assert "performance-q8-vulkan-" in build and "Candidate must not target installed runtime" in build
    assert "OwnerApproves" in switch and "OWNER_ONLY" in verify
    assert "Q8_0" in verify and "Vulkan0" in verify
