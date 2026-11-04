"""Exact PCM pause frame counts, one RIFF header, and final bounds."""
import io
import wave
import pytest
from lite.audio import AudioError,wav_info
from lite.pause import parse_pause_script
from lite.pause_audio import assemble_pause_wav

def sample(frames=2400, byte=b"\x01\x02"):
    b=io.BytesIO()
    with wave.open(b,"wb") as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000)
        w.writeframes(byte*frames)
    return b.getvalue()

@pytest.mark.parametrize("ms",[200,350,800,1500])
def test_insert_exact_silence(ms):
    script=parse_pause_script("A [pause "+str(ms)+"ms] B")
    data,meta=assemble_pause_wav(script,[sample(2400),sample(4800)])
    assert wav_info(data)["frames"]==7200+24*ms
    assert meta["speech_frames"]==7200 and meta["pause_frames"]==24*ms
    assert data.count(b"RIFF")==1
    with wave.open(io.BytesIO(data),"rb") as w:
        frames=w.readframes(w.getnframes())
    assert frames[4800:4800+ms*48]==b"\x00"*(ms*48)
    assert frames[4800+ms*48:4800+ms*48+4]==b"\x01\x02\x01\x02"

def test_leading_trailing_consecutive():
    s=parse_pause_script("[pause 200ms][pause 350ms]A[pause 800ms]")
    data,meta=assemble_pause_wav(s,[sample()])
    assert meta["speech_segments"]==1
    assert wav_info(data)["frames"]==2400+1350*24
    with wave.open(io.BytesIO(data),"rb") as w:
        raw=w.readframes(w.getnframes())
    assert raw[:(550*48)]==b"\x00"*(550*48)
    assert raw[-800*48:]==b"\x00"*(800*48)

def test_fail_before_oversize_publish():
    s=parse_pause_script("A[pause 10s]B")
    with pytest.raises(AudioError):
        assemble_pause_wav(s,[sample(),sample()],max_bytes=10000)
    with pytest.raises(AudioError):
        assemble_pause_wav(s,[sample(),sample()],max_seconds=.2)

def test_no_tag_single_wav_not_assembled():
    assert parse_pause_script("A. B") is None
    assert wav_info(sample())["frames"]==2400

def test_reject_invalid_segment():
    s=parse_pause_script("A[pause]B")
    with pytest.raises(AudioError):
        assemble_pause_wav(s,[sample(),b"not a WAV"])

def test_exact_600_second_final_limit():
    # 599 seconds of valid PCM plus two explicit seconds must be rejected.
    # The final allocation is checked before writing one enormous WAV.
    original=sample(24000*599)
    script=parse_pause_script("Speech[pause 2s]")
    with pytest.raises(AudioError):
        assemble_pause_wav(script,[original])
