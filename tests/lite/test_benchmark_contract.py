"""Frozen benchmark contract with synthetic PCM, no user audio or model downloaded."""
import hashlib
from pathlib import Path
import pytest
from lite.benchmark import TEXT,TEXT_SHA256,_device_proof,run
from lite.audio_metrics import measure
from tests.lite.test_profiles_and_audio import sample_wav

def test_frozen_case_length_and_sha():
    assert len(TEXT)==306
    assert hashlib.sha256(TEXT.encode("utf-8")).hexdigest()==TEXT_SHA256
    assert TEXT.startswith("You lift the lid, toss in a banana peel")
    assert TEXT.endswith("they might lose their jobs.")

@pytest.mark.parametrize("repetitions",[0,1,2,4])
def test_frozen_repetitions_rejected(tmp_path,repetitions):
    with pytest.raises(ValueError):
        run("voice",tmp_path,repetitions=repetitions)

def test_native_device_evidence_gate():
    proof=_device_proof({"X-Lite-Verified-Backend":"Vulkan0",
        "X-Lite-Observed-Device":"NVIDIA GeForce GTX 1650",
        "X-Lite-Verification-Status":"verified"})
    assert proof["verification_status"]=="verified"
    for wrong in ("CPU","Vulkan1"):
        with pytest.raises(ValueError):
            _device_proof({"X-Lite-Verified-Backend":wrong,
                "X-Lite-Observed-Device":"NVIDIA GeForce GTX 1650",
                "X-Lite-Verification-Status":"verified"})
    with pytest.raises(ValueError):
        _device_proof({})

def test_pcm16_report_complete(tmp_path):
    wav=tmp_path/"s.wav"
    wav.write_bytes(sample_wav())
    info=measure(wav)
    assert info["sample_rate"]==24000 and info["channels"]==1 and info["bit_depth"]==16
    assert info["duration_s"]==1
    assert "begin_silence_s" in info and "rms" in info and "clipped_frames" in info
