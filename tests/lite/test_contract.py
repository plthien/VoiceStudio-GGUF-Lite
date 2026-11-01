"""Deterministic API assertions, independent of native GPU."""
from io import BytesIO
import wave
import pytest
from fastapi.testclient import TestClient
from lite.app import create_app
from lite.config import Settings, REPO_ID
from lite.audio import wav_info, AudioError
from lite.runtime import parse_vulkan_devices

@pytest.fixture
def client(tmp_path):
    app=create_app(Settings(data_dir=tmp_path/"data",cache_dir=tmp_path/"models",
                            bin_dir=tmp_path/"bin",timeout=30))
    with TestClient(app) as c:
        yield c

def test_health_not_readiness(client):
    assert client.get("/health").status_code==200
    assert client.get("/lite/readiness").json()["ready"] is False

def test_speech_contract(client):
    base={"model":"omnivoice-gguf","voice":"unknown","input":"Hello"}
    assert client.post("/v1/audio/speech",json=base).status_code==404
    for bad in ({"model":"other"},{"speed":1.1},{"response_format":"mp3"},
                {"input":"x"*4097},{"input":"   "}):
        response=client.post("/v1/audio/speech",json=base|bad)
        assert response.status_code==422
        assert response.json()["error"]["code"]=="INVALID_REQUEST"

def test_manual_licence_and_provenance_block(client):
    status=client.get("/models/licenses/acceptance/"+REPO_ID).json()
    assert not status["accepted"]
    assert not status["provenance_reconciled"]
    accepted=client.post("/models/licenses/accept",json={
        "repo_id":REPO_ID,"fingerprint":status["fingerprint"],"accepted":True})
    assert accepted.status_code==200
    assert accepted.json()["accepted"]
    assert not accepted.json()["commercial_cleared"]
    assert not client.get("/lite/readiness").json()["ready"]
    assert client.post("/models/licenses/accept",json={
        "repo_id":REPO_ID,"fingerprint":"bad","accepted":True}).status_code==409

def test_no_aliases_or_unknown_profiles(client):
    assert client.get("/v1/audio/voices").json()==[]
    assert client.get("/profiles/bad").status_code==404
    assert client.delete("/profiles/bad").status_code==404

def test_wav_integrity():
    b=BytesIO()
    with wave.open(b,"wb") as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000)
        w.writeframes(b"\0\0"*240)
    assert wav_info(b.getvalue())["frames"]==240
    with pytest.raises(AudioError):
        wav_info(b"bad")

def test_amd_would_not_be_vulkan0_nvidia():
    gpu=parse_vulkan_devices("""GPU0:
deviceName = AMD Radeon 780M
deviceType = PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU
vendorID = 0x1002
GPU1:
deviceName = NVIDIA GeForce GTX 1650
deviceType = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
vendorID = 0x10de
""")
    assert gpu[0]["name"].startswith("AMD") and gpu[1]["index"]==1
