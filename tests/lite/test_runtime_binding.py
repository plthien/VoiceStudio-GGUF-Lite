"""Candidate runtime identity must be explicit and side-effect free."""
import json
import pytest

from lite.config import Settings
from lite.runtime_binding import binding_snapshot


def fake_settings(tmp_path):
    return Settings(tmp_path, tmp_path, tmp_path, 900)


def test_candidate_control_and_receipt_changes_bind_distinct_runs(tmp_path, monkeypatch):
    from lite import runtime_binding
    receipt=tmp_path/"performance-candidate.json"
    receipt.write_text(json.dumps({"candidate_flavor": "profiled"}))
    monkeypatch.setattr(runtime_binding, "persistent_capability",
        lambda settings: {"supported": True, "performance_protocol": True,
                          "exe_sha256": "a"*64})
    base=binding_snapshot(fake_settings(tmp_path), {})
    assert base["settings_verification"]=="not_yet_verified"
    assert base["cache_verification"]=="not_yet_verified"
    monkeypatch.setenv("GGUF_LITE_EXPERIMENT_MASKGIT_STEPS", "8")
    step=binding_snapshot(fake_settings(tmp_path), {})
    assert step["identity_sha256"]!=base["identity_sha256"]
    receipt.write_text(json.dumps({"candidate_flavor": "ref-cache"}))
    assert binding_snapshot(fake_settings(tmp_path), {})["identity_sha256"]!=step["identity_sha256"]
    monkeypatch.setenv("GGUF_LITE_EXPERIMENT_REF_CACHE", "1")
    assert binding_snapshot(fake_settings(tmp_path), {})["identity_sha256"]!=step["identity_sha256"]
    receipt.write_text(json.dumps({"candidate_flavor": "steps-32"}))
    with pytest.raises(ValueError, match="flavor"):
        binding_snapshot(fake_settings(tmp_path), {})
    monkeypatch.setenv("GGUF_LITE_EXPERIMENT_MASKGIT_STEPS", "7")
    with pytest.raises(ValueError, match="steps"):
        binding_snapshot(fake_settings(tmp_path), {})


def test_baseline_does_not_inherit_candidate_claims(tmp_path, monkeypatch):
    from lite import runtime_binding
    monkeypatch.setattr(runtime_binding, "persistent_capability",
        lambda settings: {"supported": True, "performance_protocol": False,
                          "exe_sha256": "a"*64})
    result=binding_snapshot(fake_settings(tmp_path), {})
    assert result["candidate_kind"]=="verified_baseline"
    assert result["candidate_receipt_sha256"] is None
    assert result["settings_verification"]=="not_yet_verified"


def test_actual_perf_and_native_attestation_are_required(tmp_path, monkeypatch):
    from lite import runtime_binding
    (tmp_path/"performance-candidate.json").write_text('{"candidate_flavor":"profiled"}')
    monkeypatch.setattr(runtime_binding, "persistent_capability",
        lambda settings: {"supported": True, "performance_protocol": True,
                          "exe_sha256": "a"*64})
    report={"performance":{"schema":"LitePerf/v1","maskgit_steps":32,"flash_attention":1},
            "native_proof":{"verification_status":"verified","observed_native_backend":"Vulkan0",
                            "observed_device_identity":"NVIDIA GeForce GTX 1650"},
            "native_controls":{"maskgit_steps":32,"flash_attention":1,"ref_cache_enabled":False}}
    assert binding_snapshot(fake_settings(tmp_path),report)["settings_verification"]=="observed"
    report["performance"]["flash_attention"]=0
    assert binding_snapshot(fake_settings(tmp_path),report)["settings_verification"]=="not_yet_verified"
