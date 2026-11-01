import io
import wave
from lite.audio_metrics import measure

def test_metrics_known_pcm16(tmp_path):
    path = tmp_path / "test.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x00\x00" * 2400 + b"\xff\x7f" * 2400)
    info = measure(path)
    assert info["duration_s"] == 0.2
    assert info["begin_silence_s"] == 0.1
    assert info["clipped_frames"] == 2400
    assert 0.7 < info["rms"] < 0.71
