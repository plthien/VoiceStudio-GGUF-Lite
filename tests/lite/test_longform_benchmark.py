import json,time,wave
from pathlib import Path
import pytest
from lite.longform_benchmark import (MAX_CHARS,MAX_PAUSES,MAX_ESTIMATED_SECONDS,
    assemble_wavs,create_manifest,estimated_seconds,metrics_row,split_section,write_manifest_atomic)

def test_lossless_bounded_section_split_and_manifest(tmp_path):
    text=("A complete sentence with words. "*700)+"Final word."
    parts=split_section(text)
    assert "".join(x.text for x in parts)==text
    assert all(len(x.text)<=MAX_CHARS for x in parts)
    assert all(x.text.count("[pause")<=MAX_PAUSES for x in parts)
    assert all(estimated_seconds(x.text)<=MAX_ESTIMATED_SECONDS for x in parts)
    manifest=create_manifest([text],voice_version=7,seed=42,speed=1.0)
    path=tmp_path/"manifest.json";write_manifest_atomic(path,manifest);write_manifest_atomic(path,manifest)
    changed={**manifest,"speed":1.1}
    with pytest.raises(ValueError):write_manifest_atomic(path,changed)

def test_fake_600_second_streaming_assembly_exact(tmp_path):
    paths=[]
    for index in range(10):
        path=tmp_path/f"{index}.wav";paths.append(path)
        with wave.open(str(path),"wb") as out:
            out.setnchannels(1);out.setsampwidth(2);out.setframerate(24000)
            block=b"\x01\x00"*24000
            for _ in range(60):out.writeframesraw(block)
    result=assemble_wavs(paths,tmp_path/"final.wav")
    assert result["frames"]==600*24000 and result["audio_s"]==600
    with wave.open(str(tmp_path/"final.wav"),"rb") as src:assert src.getnframes()==600*24000

def test_pause_does_not_mask_speech_rtf():
    row=metrics_row(index=0,wall_s=30,audio_s=20,speech_s=10,cold=True,
                    headers={"X-Lite-Verification-Status":"verified"})
    assert row["final_rtf"]==1.5 and row["speech_rtf"]==3

@pytest.mark.parametrize("token",["x"*4097,"[pause 1s] "*17+"end"])
def test_constraints_force_safe_split_or_reject(token):
    try: parts=split_section(token)
    except ValueError:return
    assert "".join(x.text for x in parts)==token
    assert all(len(x.text)<=4096 and x.text.lower().count("[pause")<=16 for x in parts)
