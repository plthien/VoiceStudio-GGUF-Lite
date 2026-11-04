import io,wave
from experiments.high_quality_tts.benchmark_adapter import compare,fake_synthesize,normalize_wav,probe

def test_fake_adapter_is_bounded_pcm16():
    data=fake_synthesize(2)
    with wave.open(io.BytesIO(data),"rb") as src:
        assert (src.getframerate(),src.getnchannels(),src.getsampwidth(),src.getnframes())==(24000,1,2,48000)
    assert compare("fake","test")["status"]=="COMPLETE"

def test_unavailable_engines_are_explicit(monkeypatch):
    monkeypatch.setattr("importlib.util.find_spec",lambda name:None)
    assert probe("pocket_tts").status=="RUNTIME_UNAVAILABLE"
    assert probe("chatterbox").status=="POC_UNAVAILABLE"

def test_normalize_stereo_48k_to_required_format():
    out=io.BytesIO()
    with wave.open(out,"wb") as dst:
        dst.setnchannels(2);dst.setsampwidth(2);dst.setframerate(48000);dst.writeframes(b"\x01\x00\x01\x00"*4800)
    with wave.open(io.BytesIO(normalize_wav(out.getvalue())),"rb") as src:
        assert (src.getframerate(),src.getnchannels(),src.getsampwidth())==(24000,1,2)
