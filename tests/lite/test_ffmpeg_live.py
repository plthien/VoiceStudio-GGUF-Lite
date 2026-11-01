"""Exercise real FFmpeg normalization instead of renaming MP3 to WAV."""
import io
import shutil
import subprocess
import wave
import pytest
from lite.audio import convert_reference, wav_file, AudioError

@pytest.fixture
def ffmpeg():
    command = shutil.which("ffmpeg")
    if not command:
        pytest.skip("FFmpeg not available in this test environment")
    return command

def _wav(path, *, sample_rate=48000, channels=2):
    with wave.open(str(path),"wb") as w:
        w.setframerate(sample_rate)
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.writeframes(b"\x01\x00"*sample_rate*channels)

def test_mp3_to_24k_pcm16_with_real_ffmpeg(tmp_path,ffmpeg):
    source=tmp_path/"source.wav"
    mp3=tmp_path/"source.mp3"
    out=tmp_path/"normalized.wav"
    _wav(source)
    result=subprocess.run([ffmpeg,"-hide_banner","-loglevel","error","-y",
                           "-i",str(source),"-codec:a","libmp3lame",str(mp3)],
                          capture_output=True,timeout=30)
    if result.returncode:
        pytest.skip("FFmpeg build lacks MP3 encoder")
    before=mp3.read_bytes()
    info=convert_reference(mp3,out,ffmpeg=ffmpeg,max_seconds=60)
    assert mp3.read_bytes()==before
    assert info["frames"]>20000
    with wave.open(str(out),"rb") as reader:
        assert reader.getframerate()==24000
        assert reader.getsampwidth()==2
        assert reader.getnchannels()==1

def test_mislabeled_and_corrupt_reference_rejected(tmp_path,ffmpeg):
    fake=tmp_path/"not-really-wav.wav"
    out=tmp_path/"normalized.wav"
    fake.write_bytes(b"not an audio stream")
    with pytest.raises(AudioError):
        convert_reference(fake,out,ffmpeg=ffmpeg)
    assert not out.exists()

def test_incompatible_wav_correctly_resampled(tmp_path,ffmpeg):
    source=tmp_path/"stereo-48k.wav"
    dest=tmp_path/"mono-24k.wav"
    _wav(source)
    data=source.read_bytes()
    convert_reference(source,dest,ffmpeg=ffmpeg)
    assert source.read_bytes()==data
    info=wav_file(dest)
    assert info["frames"]==24000
