"""Frozen benchmark contract with synthetic PCM, no user audio or model downloaded."""
import hashlib
from pathlib import Path
import pytest
from lite.benchmark import TEXT,TEXT_SHA256,_device_proof,run
from lite.audio_metrics import measure
import io
import wave

def sample_wav():
    b=io.BytesIO()
    with wave.open(b,"wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(24000)
        out.writeframes(b"\x01\x00"*24000)
    return b.getvalue()

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

def test_complete_three_run_synthetic_report(tmp_path,monkeypatch):
    import lite.benchmark as bench
    from lite.config import Settings
    settings=Settings(data_dir=tmp_path/"data",cache_dir=tmp_path/"models",
                      bin_dir=tmp_path/"bin",timeout=30)
    exe=tmp_path/"native.exe"
    dll=tmp_path/"ggml-vulkan.dll"
    exe.write_bytes(b"MZ"+b"x"*200)
    dll.write_bytes(b"MZ"+b"x"*200)
    monkeypatch.setattr(bench,"Settings",type("FakeSettings",(),{"load":staticmethod(lambda:settings)}))
    monkeypatch.setattr(bench,"native_files",lambda settings:(exe,dll))
    monkeypatch.setattr(bench,"_command_version",lambda *args:"synthetic")
    readiness={"ready":True,"quant":{"verified":True,"prepared_quant":"Q8_0","in_use":None},
               "gpu":{"verified":True,"name":"NVIDIA GeForce GTX 1650","backend":"Vulkan0"}}
    profile={"id":"voice","version":1,"ref_sha256":"a"*64,"source_sha256":"b"*64,
             "ref_text":"Exact user supplied transcript","language":"en"}
    monkeypatch.setattr(bench,"_json",lambda url:profile if "/profiles/" in url else readiness)
    calls=[]
    def fake_http(url,body=None):
        import time
        time.sleep(0.03)  # Real elapsed time even on coarser Windows clocks.
        calls.append(body)
        return sample_wav(),{"X-Lite-Verified-Backend":"Vulkan0",
            "X-Lite-Observed-Device":"NVIDIA GeForce GTX 1650",
            "X-Lite-Verification-Status":"verified"}
    monkeypatch.setattr(bench,"_http",fake_http)
    result=bench.run("voice",tmp_path/"benchmarks",port=3900,repetitions=3)
    assert result["status"]=="3_OF_3_NATIVE_VERIFIED"
    assert len(result["runs"])==len(calls)==3
    assert all(c["input"]==TEXT and c["seed"]==42 and c["voice"]=="voice" for c in calls)
    assert result["mean_rtf"]>0 and result["median_rtf"]>0
    assert result["comparability"] is False
    assert result["ref_audio_sha256"]=="b"*64
    assert (tmp_path/"benchmarks"/"benchmark.json").exists()
    assert all((tmp_path/"benchmarks"/("run-%02d.wav"%i)).exists() for i in range(1,4))
