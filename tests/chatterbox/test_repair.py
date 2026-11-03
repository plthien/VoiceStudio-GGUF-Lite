"""PR2 repair contract tests: synthetic CI evidence, never hardware qualification."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from lite.chatterbox_assembly import do_job, load_source, output_job_lock
from lite.chatterbox_benchmark import evaluate_h07
from lite.chatterbox_config import ChatterboxSettings, MODEL_REVISION
from lite.chatterbox_service import TurboService, TurboError
from lite.audio import wav_info
from .test_contracts import api, FakeTurbo, audio, profile, gen


@pytest.mark.parametrize("audio_s,wall_s,expected", [
    (600,1799,"PASS"), (600,1800,"PASS"), (600,1801,"FAIL"),
    (599.999,1200,"NOT_VERIFIED"), (600,1800.001,"FAIL"),
    (610,1800,"PASS"), (600,1799.999,"PASS"),
])
def test_h07_exact_boundaries(audio_s,wall_s,expected):
    result=evaluate_h07(audio_s,wall_s)
    assert result["h07_status"]==expected
    assert result["end_to_end_rtf"]==pytest.approx(wall_s/audio_s)
    assert result["cold_start_included"] is True


def test_h07_cold_not_included():
    assert evaluate_h07(600,1000,False)["h07_status"]=="NOT_VERIFIED"


@pytest.mark.parametrize("audio_s,wall_s,expected", [
    (600,1799,0), (600,1800,0), (600,1801,1), (599,800,1),
])
def test_benchmark_cli_json_csv_and_exit(tmp_path,monkeypatch,audio_s,wall_s,expected):
    import lite.chatterbox_benchmark as bench
    manifest=tmp_path/"source.json"
    manifest.write_text(json.dumps({"voice":"test-voice","sections":[{"id":"s1","input":"Text"}]}))
    output=tmp_path/"benchmark"
    calls=[]
    class Reply:
        status=200
        def __enter__(self):return self
        def __exit__(self,*args):return False
    def fake_urlopen(req,**kwargs):
        calls.append(req.full_url)
        return Reply()
    monkeypatch.setattr(bench.urllib.request,"urlopen",fake_urlopen)
    monkeypatch.setattr(bench,"do_job",lambda *args,**kwargs:
        {"state":"PASS","audio_s":audio_s,"sections":1,"voice_version":2,
         "model_revision":MODEL_REVISION})
    ticks=iter([100,100+wall_s])
    import types
    monkeypatch.setattr(bench,"time",types.SimpleNamespace(monotonic=lambda:next(ticks)))
    monkeypatch.setattr(sys,"argv",["benchmark","--mode","LongForm","--voice-id","test-voice",
                                   "--manifest",str(manifest),"--output",str(output)])
    assert bench.main()==expected
    assert calls==["http://127.0.0.1:3901/chatterbox/runtime/unload"]
    rows=json.loads((output/"owner-metrics.json").read_text())
    assert rows[0]["generation_state"]=="PASS"
    assert rows[0]["h07_status"]==("PASS" if expected==0 else
                                    "NOT_VERIFIED" if audio_s<600 else "FAIL")
    assert rows[0]["voice_version"]==2 and rows[0]["model_revision"]==MODEL_REVISION
    csv_text=(output/"owner-metrics.csv").read_text()
    assert "h07_status" in csv_text and "end_to_end_rtf" in csv_text
    assert rows[0]["cold_start_included"] is True


def test_benchmark_wrong_voice_never_calls_runtime(tmp_path,monkeypatch):
    import lite.chatterbox_benchmark as bench
    manifest=tmp_path/"private.json"
    manifest.write_text(json.dumps({"voice":"different","sections":[{"id":"x","input":"Text"}]}))
    output=tmp_path/"bench"
    def forbidden(*args,**kwargs):
        pytest.fail("mismatched voice must not trigger HTTP requests")
    monkeypatch.setattr(bench.urllib.request,"urlopen",forbidden)
    monkeypatch.setattr(sys,"argv",["benchmark","--mode","All","--voice-id","selected",
                                   "--manifest",str(manifest),"--output",str(output)])
    assert bench.main()==1
    row=json.loads((output/"owner-metrics.json").read_text())[0]
    assert row["error"]=="BENCHMARK_VOICE_MISMATCH"
    assert not (output/"longform").exists()
    assert "BENCHMARK_VOICE_MISMATCH" in (output/"owner-metrics.csv").read_text()


@pytest.mark.parametrize("voice",["",None,42,[],{}])
def test_invalid_manifest_voice_never_emits_artifacts(tmp_path,voice):
    manifest=tmp_path/"source.json"
    manifest.write_text(json.dumps({"voice":voice,"sections":[{"id":"sec","input":"ok"}]}))
    output=tmp_path/"output"
    with pytest.raises(ValueError,match="BENCHMARK_VOICE_INVALID"):
        do_job(manifest,output,expected_voice="selected")
    assert not output.exists()


def test_kernel_cross_process_lock_and_crash_recovery(tmp_path):
    output=tmp_path/"one-job"
    code=("from pathlib import Path\n"
          "from lite.chatterbox_assembly import output_job_lock\n"
          "import sys\n"
          "with output_job_lock(Path(sys.argv[1])):\n"
          " print('HELD',flush=True)\n"
          " sys.stdin.readline()\n")
    root=Path(__file__).resolve().parents[2]
    proc=subprocess.Popen([sys.executable,"-c",code,str(output)],cwd=root,
                          stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                          text=True)
    try:
        assert proc.stdout.readline().strip()=="HELD"
        with pytest.raises(RuntimeError,match="OUTPUT_JOB_BUSY"):
            with output_job_lock(output):pass
    finally:
        proc.kill();proc.wait(timeout=10)
    with output_job_lock(output):
        assert (output/".chatterbox-job.lock").exists()


def test_profile_change_does_not_release_admission_before_cache_reset(api,monkeypatch):
    client,service,_=api
    voice=profile(client)["id"]
    entered=threading.Event();release=threading.Event()
    original=service.invalidate
    async def gated_invalidate(which=None):
        entered.set()
        assert await asyncio.to_thread(release.wait,5)
        await original(which)
    monkeypatch.setattr(service,"invalidate",gated_invalidate)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(client.put,"/profiles/"+voice+"/audio",
            data={"ref_text":"New authorized transcript","consent":"confirmed"},
            files={"ref_audio":("new.wav",audio(6.5),"audio/wav")})
        try:
            assert entered.wait(10)
            assert gen(client,voice).status_code==429
            assert client.get("/profiles/"+voice).json()["version"]==2
        finally:
            release.set()
        assert future.result(timeout=10).status_code==200
    assert gen(client,voice).status_code==200
    assert service.history[-1][1]==2


def test_invalidation_failure_after_commit_reaps_worker(api,monkeypatch):
    client,service,_=api
    voice=profile(client)["id"]
    assert gen(client,voice).status_code==200
    original=service.invalidate
    async def broken(_voice=None):
        raise TurboError("WORKER_CRASH")
    monkeypatch.setattr(service,"invalidate",broken)
    reply=client.put("/profiles/"+voice+"/audio",
           data={"ref_text":"Updated consented reference","consent":"confirmed"},
           files={"ref_audio":("updated.wav",audio(6.5),"audio/wav")})
    assert reply.status_code==503
    assert reply.json()["error"]["code"]=="MUTATION_COMMITTED_CACHE_RESET"
    assert client.get("/profiles/"+voice).json()["version"]==2
    assert service.state=="UNLOADED" and service.cache==set()
    monkeypatch.setattr(service,"invalidate",original)
    assert gen(client,voice).status_code==200
    assert service.history[-1][1]==2


def test_idle_persist_error_does_not_change_runtime(api,monkeypatch):
    import lite.chatterbox_app as appmod
    client,service,settings=api
    assert service.idle_timeout==600
    original=appmod.atomic
    def fail_write(*args,**kwargs):
        raise OSError("synthetic fsync/replace failure")
    monkeypatch.setattr(appmod,"atomic",fail_write)
    r=client.put("/chatterbox/runtime/settings",json={"idle_timeout_seconds":300})
    assert r.status_code==500 and r.json()["error"]["code"]=="SETTINGS_SAVE_FAILED"
    assert service.idle_timeout==600
    assert not (settings.data_dir/"chatterbox-settings.json").exists()
    monkeypatch.setattr(appmod,"atomic",original)
    def timer_arm_fail(value):
        service.idle_timeout=value
        if value==1800:raise RuntimeError("Synthetic scheduling failure")
    original_idle=service.set_idle
    monkeypatch.setattr(service,"set_idle",timer_arm_fail)
    r=client.put("/chatterbox/runtime/settings",json={"idle_timeout_seconds":1800})
    assert r.status_code==500 and service.idle_timeout==600
    assert not (settings.data_dir/"chatterbox-settings.json").exists()
    monkeypatch.setattr(service,"set_idle",original_idle)
    assert client.put("/chatterbox/runtime/settings",json={"idle_timeout_seconds":300}).status_code==200
    assert json.loads((settings.data_dir/"chatterbox-settings.json").read_text())["idle_timeout_seconds"]==300


def test_timeout_during_final_assembly_has_no_wav(api,monkeypatch):
    import lite.chatterbox_service as service_mod
    client,service,_=api
    voice=profile(client)["id"]
    real=service_mod.finalize_audio
    def slow(*args):
        time.sleep(1)
        return real(*args)
    monkeypatch.setattr(service_mod,"finalize_audio",slow)
    r=gen(client,voice,"First part. [pause 250ms] Second part.")
    assert r.status_code==504 and r.json()["error"]["code"]=="DEADLINE_EXCEEDED"
    assert not r.content.startswith(b"RIFF")
    assert service.state=="ERROR"


def test_real_supervisor_with_framed_fake_subprocess(tmp_path,monkeypatch):
    """Real TurboService, real OS child and framed stdin/stdout; synthetic audio."""
    from lite.chatterbox_service import TurboService
    settings=ChatterboxSettings(root=tmp_path,timeout=6)
    monkeypatch.setattr(ChatterboxSettings,"venv_python",property(lambda self: Path(sys.executable)))
    original=asyncio.create_subprocess_exec
    fixture=Path(__file__).with_name("subprocess_worker.py")
    async def spawn(*args,**kwargs):
        return await original(sys.executable,str(fixture),**kwargs)
    monkeypatch.setattr(asyncio,"create_subprocess_exec",spawn)
    async def no_gguf(self,deadline):return None
    monkeypatch.setattr(TurboService,"_assert_no_gguf",no_gguf)
    async def exercise():
        service=TurboService(settings)
        profile={"id":"synthetic","version":1,"ref_sha256":"test"}
        ref=tmp_path/"not-read-by-fake.wav"
        try:
            first,info=await service.synthesize(profile=profile,reference=ref,
                                                text="Hello",script=None,speed=1,seed=42)
            assert wav_info(first)["frames"]==6000
            second,other=await service.synthesize(profile=profile,reference=ref,
                                                  text="Again",script=None,speed=1,seed=42)
            assert wav_info(second)["frames"]==6000
            assert service.loads==1 and service.syntheses==2 and service.hits==1
            proc=service.proc
            assert proc is not None and proc.returncode is None
        finally:
            await service.stop()
        assert proc.returncode is not None
        assert service.proc is None
    asyncio.run(exercise())


def test_engine_conditions_cached_without_torch_or_model_download(tmp_path,monkeypatch):
    import lite.chatterbox_worker as worker
    settings=ChatterboxSettings(root=tmp_path)
    monkeypatch.setattr(worker.ChatterboxSettings,"load",classmethod(lambda cls:settings))
    class Cond:
        def __init__(self,owner):self.owner=owner
        def to(self,device):return self
    class Model:
        def __init__(self):self.conds=None;self.trace=[]
        def prepare_conditionals(self,path):
            self.conds=Cond(Path(path).parent.parent.name)
        def generate(self,text):
            self.trace.append(self.conds.owner)
            return None
    class Cuda:
        OutOfMemoryError=MemoryError
        def manual_seed_all(self,value):pass
        def mem_get_info(self):return 100,200
        def memory_allocated(self):return 12
        def memory_reserved(self):return 20
        def max_memory_allocated(self):return 30
    class Torch:
        cuda=Cuda()
        def manual_seed(self,value):pass
        def inference_mode(self):return contextlib.nullcontext()
    monkeypatch.setattr(worker,"pcm_wav",lambda data:audio(.25))
    engine=worker.Engine()
    engine.model=Model();engine.torch=Torch()
    for owner in ("A","B"):
        ref=settings.data_dir/"voices"/owner/"1"/"reference.wav"
        ref.parent.mkdir(parents=True)
        ref.write_bytes(audio(6))
    def synth(owner,version):
        ref=settings.data_dir/"voices"/owner/"1"/"reference.wav"
        digest=hashlib.sha256(ref.read_bytes()).hexdigest()
        return engine.generate({"voice":owner,"version":version,"reference":str(ref),
                 "reference_sha":digest,"seed":42,"text":"Synthetic"})
    synth("A",1);synth("B",1);synth("A",1)
    assert engine.model.trace==["A","B","A"]
    assert engine.hits==1
    engine.invalidate("A")
    assert all(key[0]!="A" for key in engine.cache)
    synth("A",2)
    assert engine.model.trace[-1]=="A"
    assert ("A",2,hashlib.sha256((settings.data_dir/"voices"/"A"/"1"/"reference.wav").read_bytes()).hexdigest(),MODEL_REVISION) in engine.cache


@pytest.mark.parametrize("text,code",[
    ("__CI_BAD_FRAME__","WORKER_PROTOCOL_INVALID"),
    ("__CI_TRUNCATED__","WORKER_CRASH"),
    ("__CI_CRASH__","WORKER_CRASH"),
    ("__CI_TIMEOUT__","DEADLINE_EXCEEDED"),
])
def test_actual_subprocess_failure_is_typed_and_reaped(tmp_path,monkeypatch,text,code):
    settings=ChatterboxSettings(root=tmp_path,timeout=8)
    monkeypatch.setattr(ChatterboxSettings,"venv_python",property(lambda self:Path(sys.executable)))
    original=asyncio.create_subprocess_exec
    fixture=Path(__file__).with_name("subprocess_worker.py")
    async def spawn(*args,**kwargs):
        return await original(sys.executable,str(fixture),**kwargs)
    monkeypatch.setattr(asyncio,"create_subprocess_exec",spawn)
    async def no_gguf(self,deadline):return None
    monkeypatch.setattr(TurboService,"_assert_no_gguf",no_gguf)
    async def scenario():
        svc=TurboService(settings)
        row={"id":"synthetic","version":1,"ref_sha256":"test"}
        reference=tmp_path/"placeholder.wav"
        await svc.synthesize(profile=row,reference=reference,text="Warm",
                             script=None,speed=1.0,seed=42)
        proc=svc.proc
        assert proc and proc.returncode is None
        svc.settings=ChatterboxSettings(root=tmp_path,timeout=1.5)
        with pytest.raises(TurboError,match=code):
            await svc.synthesize(profile=row,reference=reference,text=text,
                                 script=None,speed=1.0,seed=42)
        assert svc.state=="ERROR" and svc.proc is None
        assert proc.returncode is not None
    asyncio.run(scenario())


def test_spawn_failure_typed_and_never_replayed(tmp_path,monkeypatch):
    settings=ChatterboxSettings(root=tmp_path,timeout=8)
    monkeypatch.setattr(ChatterboxSettings,"venv_python",property(lambda self:Path(sys.executable)))
    async def no_gguf(self,deadline):return None
    monkeypatch.setattr(TurboService,"_assert_no_gguf",no_gguf)
    count=[0]
    async def fail_spawn(*args,**kwargs):
        count[0]+=1
        raise OSError("Synthetic permission denied")
    monkeypatch.setattr(asyncio,"create_subprocess_exec",fail_spawn)
    async def scenario():
        svc=TurboService(settings)
        with pytest.raises(TurboError,match="WORKER_START_FAILED"):
            await svc.synthesize(profile={"id":"test","version":1,"ref_sha256":"x"},
                reference=tmp_path/"ref.wav",text="Hi",script=None,speed=1.0,seed=42)
        assert count[0]==1 and svc.state=="ERROR" and svc.proc is None
    asyncio.run(scenario())


def test_hash_recorded_does_not_mean_model_verified(tmp_path):
    from lite.chatterbox_config import diagnostic,SOURCE_REVISION,REQUIRED_FILES
    settings=ChatterboxSettings(root=tmp_path)
    settings.model_dir.mkdir(parents=True)
    settings.venv_python.parent.mkdir(parents=True)
    settings.venv_python.touch()
    (settings.model_dir/"manifest.json").write_text(json.dumps({
        "revision":MODEL_REVISION,"source_revision":SOURCE_REVISION,
        "sha256":{name:"not-a-real-hash" for name in REQUIRED_FILES}}))
    state=diagnostic(settings)
    assert state["installed"] is True and state["model_manifest_recorded"] is True
    assert state["model_hash_verified"] is False and state["ready"] is False


def test_symlink_reference_escape_rejected(api,tmp_path):
    c,svc,settings=api
    row=profile(c)
    original=settings.data_dir/"voices"/row["ref_path"]
    outside=tmp_path/"outside.wav"
    outside.write_bytes(original.read_bytes())
    original.unlink()
    try:
        original.symlink_to(outside)
    except (OSError,NotImplementedError):
        pytest.skip("Windows symlink creation privileges unavailable")
    r=gen(c,row["id"])
    assert r.status_code==409 and r.json()["error"]["code"]=="REFERENCE_INVALID"


def test_bad_content_length(api):
    c,svc,_=api
    r=c.post("/v1/audio/speech",data=b"{}",headers={
        "content-type":"application/json","content-length":"invalid-length"})
    assert r.status_code==400 and r.json()["error"]["code"]=="BAD_LENGTH"


def test_atempo_timeout_is_typed_and_reaps_no_partial_wav(api,monkeypatch):
    import lite.chatterbox_service as mod
    client,service,_=api
    voice=profile(client)["id"]
    async def expires(*args,**kwargs):
        raise asyncio.TimeoutError()
    monkeypatch.setattr(mod,"stretch_speech_async",expires)
    result=gen(client,voice,"Speech segment.",speed=1.15)
    assert result.status_code==504
    assert result.json()["error"]["code"]=="DEADLINE_EXCEEDED"
    assert not result.content.startswith(b"RIFF")
    assert service.state=="ERROR"
