import io,wave
from experiments.high_quality_tts.benchmark_adapter import compare,fake_synthesize,normalize_wav,probe

def test_isolated_fake_engine_completes_without_production_integration():
    report=compare("fake","fixed comparison sentence")
    assert report["status"]=="COMPLETE" and report["format"]=="pcm16-mono-24000"
    assert report["speaker_likeness"]=="OWNER TEST PENDING"

def test_isolated_normalizer_outputs_24k_mono_pcm16():
    data=fake_synthesize(.25)
    with wave.open(io.BytesIO(normalize_wav(data)),"rb") as src:
        assert (src.getframerate(),src.getnchannels(),src.getsampwidth())==(24000,1,2)

def test_optional_engines_report_unavailable_without_download(monkeypatch):
    monkeypatch.setattr("importlib.util.find_spec",lambda _:None)
    assert probe("pocket_tts").status=="RUNTIME_UNAVAILABLE"
    assert probe("chatterbox").status=="POC_UNAVAILABLE"
