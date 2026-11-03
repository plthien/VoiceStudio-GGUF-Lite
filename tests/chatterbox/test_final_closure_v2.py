"""V2 final closure: synthetic audio and offline provenance/installer tests only.

No model, Torch, CUDA or private voice inference is used by these tests.
"""
from __future__ import annotations
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import wave

import pytest

from lite.audio import AudioError, wav_info
from lite.chatterbox_audio import concatenate, FINAL_MAX_BYTES, FINAL_MAX_SECONDS
from lite.chatterbox_assembly import do_job, reference_binding
from lite.chatterbox_benchmark import evaluate_h07
from lite.chatterbox_config import ChatterboxSettings, MODEL_REVISION
from .test_contracts import audio


def pcm(seconds, sample=1024):
    out=io.BytesIO()
    with wave.open(out, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(24000)
        piece=sample.to_bytes(2,"little",signed=True)
        remaining=int(seconds*24000)
        while remaining:
            chunk=min(remaining,24000)
            writer.writeframesraw(piece*chunk)
            remaining-=chunk
    return out.getvalue()


@pytest.mark.parametrize("seconds", [599,600,601,610,900,901])
def test_final_audio_limits_real_pcm_assembly(seconds):
    """Actual WAV concatenation, not a mocked H07 mathematical evaluator."""
    first=min(seconds,450)
    second=seconds-first
    fragments=[pcm(first,1111)]
    if second:
        fragments.append(pcm(second,2222))
    if seconds>900:
        with pytest.raises(AudioError,match="FINAL_AUDIO_LIMIT_EXCEEDED"):
            concatenate(fragments)
        return
    final=concatenate(fragments)
    info=wav_info(final,max_bytes=FINAL_MAX_BYTES,max_seconds=FINAL_MAX_SECONDS)
    assert info["frames"]==seconds*24000
    assert info["sha256"]==hashlib.sha256(final).hexdigest()
    with wave.open(io.BytesIO(final),"rb") as reader:
        before=reader.readframes(first*24000)
        assert before==b"\x57\x04"*(first*24000)
        if second:
            after=reader.readframes(second*24000)
            assert after==b"\xae\x08"*(second*24000)
    if seconds>600:
        with pytest.raises(AudioError):wav_info(final)  # unchanged API ceiling
    if seconds>=600:
        assert evaluate_h07(seconds,1000)["h07_status"]=="PASS"


def fake_job_endpoint(monkeypatch,section_audio,profile=None,on_speech=None):
    """Wire actual do_job and finalization to synthetic HTTP endpoint responses."""
    import lite.chatterbox_assembly as asm
    row=profile if profile is not None else {"id":"owned","version":1,
        "ref_sha256":"a"*64,"source_sha256":"b"*64}
    sent=[]
    def get_json(_base,path,*args,**kwargs):
        if path.endswith("/runtime/status"):return {"busy":False}
        return dict(row)
    class Response:
        def __init__(self,data):
            self.data=data
            self.headers={"X-TTS-Backend":"SYNTHETIC-NON-CUDA",
                          "X-Lite-Pause-Count":"0"}
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,size):return self.data[:size]
    def post(request,**kwargs):
        body=json.loads(request.data)
        sent.append(body)
        if on_speech:on_speech(len(sent),row)
        return Response(section_audio[len(sent)-1])
    monkeypatch.setattr(asm,"http_json",get_json)
    monkeypatch.setattr(asm.urllib.request,"urlopen",post)
    return row,sent


@pytest.mark.parametrize("duration", [601,610,900,901])
def test_actual_mock_http_job_longform_over_six_hundred(tmp_path,monkeypatch,duration):
    parts=[450,duration-450]
    content=[pcm(parts[0],1111),pcm(parts[1],2222)]
    fake_job_endpoint(monkeypatch,content)
    manifest=tmp_path/"input.json"
    manifest.write_text(json.dumps({"voice":"owned","sections":[
        {"id":"s1","input":"First speech"},
        {"id":"s2","input":"Second speech"}]}))
    output=tmp_path/"output"
    if duration>900:
        with pytest.raises(AudioError,match="FINAL_AUDIO_LIMIT_EXCEEDED"):
            do_job(manifest,output,base="http://127.0.0.1:3901")
        assert not (output/"final.wav").exists()
        assert not (output/"summary.json").exists()
    else:
        result=do_job(manifest,output,base="http://127.0.0.1:3901")
        final=(output/"final.wav").read_bytes()
        assert result["audio_s"]==duration
        assert result["final_sha256"]==hashlib.sha256(final).hexdigest()
        assert wav_info(final,max_bytes=FINAL_MAX_BYTES,max_seconds=900)["frames"]==duration*24000
        assert evaluate_h07(result["audio_s"],900)["h07_status"]=="PASS"


def test_reference_changed_after_post_no_publish(tmp_path,monkeypatch):
    sections=[pcm(.5,123),pcm(.5,456)]
    def mutate(n,row):
        if n==1:row["ref_sha256"]="c"*64
    row,sent=fake_job_endpoint(monkeypatch,sections,on_speech=mutate)
    manifest=tmp_path/"source.json"
    manifest.write_text(json.dumps({"voice":"owned","sections":[
        {"id":"a","input":"First"},{"id":"b","input":"Second"}]}))
    output=tmp_path/"out"
    with pytest.raises(RuntimeError,match="RESUME_PROVENANCE_CHANGED"):
        do_job(manifest,output)
    assert len(sent)==1
    assert not (output/"final.wav").exists()
    assert not (output/"summary.json").exists()
    assert row["version"]==1


def test_reference_changed_without_version_on_resume(tmp_path,monkeypatch):
    row,sent=fake_job_endpoint(monkeypatch,[pcm(.5)])
    manifest=tmp_path/"source.json"
    manifest.write_text(json.dumps({"voice":"owned","sections":[{"id":"a","input":"First"}]}))
    out=tmp_path/"result"
    do_job(manifest,out)
    assert len(sent)==1
    row["source_sha256"]="c"*64
    with pytest.raises(RuntimeError,match="RESUME_PROVENANCE_CHANGED"):
        do_job(manifest,out,resume=True)
    assert len(sent)==1
    assert reference_binding(row,"owned") != json.loads(
        (out/"resume-manifest.json").read_text())["reference_binding"]


def test_reference_fingerprint_validates_fields_and_is_opaque():
    row={"id":"owned","version":1,"ref_sha256":"a"*64,"source_sha256":"b"*64}
    digest=reference_binding(row,"owned")
    assert len(digest)==64 and row["ref_sha256"]!=digest
    assert row["source_sha256"] not in digest
    row["source_sha256"]="c"*64
    assert reference_binding(row,"owned")!=digest
    with pytest.raises(RuntimeError,match="REFERENCE_PROVENANCE_UNAVAILABLE"):
        reference_binding({"id":"owned","version":1},"owned")


def test_multiple_fresh_longform_benchmarks_are_cold_archived(tmp_path,monkeypatch):
    import lite.chatterbox_benchmark as bench
    manifest=tmp_path/"input.json"
    manifest.write_text(json.dumps({"voice":"owned","sections":[{"id":"s1","input":"Text"}]}))
    root=tmp_path/"bench"
    unloaded=[]
    def fake_unload(req,**kwargs):
        unloaded.append(req.full_url)
        class OK:
            status=200
            def __enter__(self):return self
            def __exit__(self,*args):pass
        return OK()
    monkeypatch.setattr(bench.urllib.request,"urlopen",fake_unload)
    def fake_job(_manifest,out,*args,**kwargs):
        out.mkdir(parents=True,exist_ok=False)
        (out/"final.wav").write_bytes(pcm(.1))
        return {"state":"PASS","audio_s":610,"voice_version":1,"model_revision":MODEL_REVISION}
    monkeypatch.setattr(bench,"do_job",fake_job)
    monkeypatch.setattr(sys,"argv",["benchmark","--mode","LongForm",
                                   "--voice-id","owned","--manifest",str(manifest),
                                   "--output",str(root)])
    assert bench.main()==0
    first=json.loads((root/"owner-metrics.json").read_text())[0]
    first_archive=root/first["run_directory"]
    original=(first_archive/"owner-metrics.json").read_bytes()
    assert bench.main()==0
    second=json.loads((root/"owner-metrics.json").read_text())[0]
    assert first["run_id"]!=second["run_id"]
    assert len(unloaded)==2
    assert first_archive.joinpath("longform","final.wav").exists()
    assert (root/second["run_directory"]/"longform"/"final.wav").exists()
    assert (first_archive/"owner-metrics.json").read_bytes()==original
    assert second["h07_status"]=="PASS"


def test_cli_verify_is_nonzero_without_model_and_no_gpu_claim(tmp_path,monkeypatch):
    import lite.chatterbox_config as cfg
    settings=ChatterboxSettings(root=tmp_path)
    monkeypatch.setattr(cfg.ChatterboxSettings,"load",classmethod(lambda cls:settings))
    monkeypatch.setattr(sys,"argv",["python","--verify"])
    assert cfg.main()==1
    assert cfg.diagnostic(settings)["ready"] is False
    monkeypatch.setattr(sys,"argv",["python","--cuda-probe"])
    assert cfg.main()==1


def test_cli_synthetic_valid_hashes_only_preflight(tmp_path,monkeypatch):
    import lite.chatterbox_config as cfg
    settings=ChatterboxSettings(root=tmp_path)
    settings.venv_python.parent.mkdir(parents=True)
    settings.venv_python.touch()
    settings.model_dir.mkdir(parents=True)
    digests={}
    for name in cfg.REQUIRED_FILES:
        content=("synthetic:"+name).encode()
        (settings.model_dir/name).write_bytes(content)
        digests[name]=hashlib.sha256(content).hexdigest()
    (settings.model_dir/"manifest.json").write_text(json.dumps({
        "revision":MODEL_REVISION,"source_revision":cfg.SOURCE_REVISION,"sha256":digests}))
    monkeypatch.setattr(cfg,"MODEL_HASHES",{k:digests[k] for k in cfg.MODEL_HASHES})
    monkeypatch.setattr(cfg.ChatterboxSettings,"load",classmethod(lambda cls:settings))
    monkeypatch.setattr(shutil,"which",lambda command: "/test/ffmpeg")
    assert cfg.verify_model_files(settings)==(True,[])
    monkeypatch.setattr(sys,"argv",["python","--verify"])
    assert cfg.main()==0
    assert cfg.diagnostic(settings)["ready"] is False
    (settings.model_dir/"ve.safetensors").write_bytes(b"tampered")
    assert cfg.main()==1


@pytest.mark.skipif(sys.platform!="win32",reason="real Windows PS5.1/7 only")
@pytest.mark.parametrize("shell",["powershell.exe","pwsh.exe"])
def test_offline_installer_windows_sha_and_verify_negative(tmp_path,shell):
    exe=shutil.which(shell)
    if not exe:pytest.skip("Shell unavailable")
    script=Path(__file__).resolve().parents[2]/"scripts"/"internal"/"chatterbox-sha256.ps1"
    installer=script.parent.parent/"install-chatterbox-turbo.ps1"
    sample=tmp_path/"abc.bin";sample.write_bytes(b"abc")
    escaped=str(sample).replace("'","''")
    command=f". '{str(script).replace(chr(39),chr(39)*2)}'; Get-ChatterboxSha256 -LiteralPath '{escaped}'"
    result=subprocess.run([exe,"-NoProfile","-NonInteractive","-Command",command],
                          capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip().lower()==hashlib.sha256(b"abc").hexdigest()
    wrong=subprocess.run([exe,"-NoProfile","-NonInteractive","-ExecutionPolicy","Bypass",
        "-File",str(installer),"-VerifyOnly","-Root",str(tmp_path)],
        capture_output=True,text=True,timeout=30)
    assert wrong.returncode!=0
    assert "Root must be" in wrong.stdout+wrong.stderr


def test_generation_cuda_peak_is_reset_between_requests(tmp_path,monkeypatch):
    """CI fake: generation 2 must not inherit the larger generation 1 high water."""
    import lite.chatterbox_worker as worker
    settings=ChatterboxSettings(root=tmp_path)
    monkeypatch.setattr(worker.ChatterboxSettings,"load",classmethod(lambda cls:settings))
    class Cond:
        def to(self,device):return self
    class Cuda:
        OutOfMemoryError=MemoryError
        def __init__(self):self.peak=0;self.resets=0
        def reset_peak_memory_stats(self):self.resets+=1;self.peak=15
        def manual_seed_all(self,seed):pass
        def mem_get_info(self):return (1000,2000)
        def memory_allocated(self):return 10
        def memory_reserved(self):return 20
        def max_memory_allocated(self):return self.peak
    cuda=Cuda()
    class Torch:
        def __init__(self):self.cuda=cuda
        def manual_seed(self,seed):pass
        def inference_mode(self):return contextlib.nullcontext()
    class Model:
        conds=None
        calls=0
        def prepare_conditionals(self,path):self.conds=Cond()
        def generate(self,text):
            self.calls+=1
            cuda.peak=120 if self.calls==1 else 55
            return None
    monkeypatch.setattr(worker,"pcm_wav",lambda _tensor:audio(.25))
    engine=worker.Engine()
    engine.model=Model();engine.torch=Torch()
    reference=settings.data_dir/"voices"/"A"/"1"/"reference.wav"
    reference.parent.mkdir(parents=True)
    reference.write_bytes(audio(6))
    body={"voice":"A","version":1,"reference":str(reference),
          "reference_sha":hashlib.sha256(reference.read_bytes()).hexdigest(),
          "text":"Test","seed":42}
    one=engine.generate(body)["memory"]
    two=engine.generate(body)["memory"]
    assert cuda.resets==2
    assert one["peak_bytes"]==120 and one["lifetime_peak_bytes"]==120
    assert two["peak_bytes"]==55 and two["lifetime_peak_bytes"]==120
    assert one["peak_scope"]=="generation" and two["peak_scope"]=="generation"
    assert two["allocated_bytes"]==10 and two["reserved_bytes"]==20
    assert two["free_bytes"]==1000


def test_pcm_nonfinite_tensor_fails_without_publishing():
    import lite.chatterbox_worker as worker
    import numpy as np
    class FakeTensor:
        def detach(self):return self
        def float(self):return self
        def cpu(self):return self
        def reshape(self,*args):return self
        def clamp(self,*args):return self
        def numpy(self):return np.array([0.0,np.nan],dtype=np.float32)
    with pytest.raises(RuntimeError,match="NONFINITE_AUDIO"):
        worker.pcm_wav(FakeTensor())


def test_frozen_reference_source_after_first_record_failure_on_resume(tmp_path,monkeypatch):
    rows=[pcm(.5)]
    row,requests=fake_job_endpoint(monkeypatch,rows)
    manifest=tmp_path/"request.json"
    manifest.write_text(json.dumps({"voice":"owned","sections":[{"id":"s","input":"Voice"}]}))
    out=tmp_path/"output"
    do_job(manifest,out)
    assert len(requests)==1
    row["ref_sha256"]="c"*64
    with pytest.raises(RuntimeError,match="RESUME_PROVENANCE_CHANGED"):
        do_job(manifest,out,resume=True)
    assert len(requests)==1


def test_final_summary_write_error_unpublishes_final(tmp_path,monkeypatch):
    import lite.chatterbox_assembly as asm
    fake_job_endpoint(monkeypatch,[pcm(.5)])
    source=tmp_path/"m.json"
    source.write_text(json.dumps({"voice":"owned","sections":[{"id":"s","input":"Test"}]}))
    dest=tmp_path/"dest"
    original=asm.atomic
    def fail_summary(path,data):
        if path.name=="summary.json":
            raise OSError("Injected summary publish failure")
        return original(path,data)
    monkeypatch.setattr(asm,"atomic",fail_summary)
    with pytest.raises(OSError,match="Injected"):
        do_job(source,dest)
    assert not (dest/"final.wav").exists()
    assert not (dest/"summary.json").exists()
