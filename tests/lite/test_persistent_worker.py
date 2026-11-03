import asyncio
import json
import os
import struct
import sys
import textwrap
import time
import wave
from pathlib import Path

import pytest

from lite.config import Settings
from lite.pause import parse_pause_script
from lite.resident_worker import ResidentWorkerSupervisor, remaining
from lite.native_runner import NativeBusy, NativeFailure, NativeTimeout
from lite.runtime import RuntimeErrorLite
from lite.worker_settings import (ALLOWED_IDLE_TIMEOUTS, WorkerSettings,
                                  WorkerSettingsError, WorkerSettingsStore)

FAKE = r'''
import os,struct,sys,time
H=struct.Struct("<IHHII");M=0x3150544c
ctx=0x1234abcd;init=0;synth=0
def send(c,s,b=b''):
 sys.stdout.buffer.write(H.pack(M,1,c,s,len(b))+b);sys.stdout.buffer.flush()
def field(p,a):
 n=struct.unpack_from('<I',p,a)[0];a+=4;return p[a:a+n],a+n
while True:
 h=sys.stdin.buffer.read(H.size)
 if not h: break
 magic,v,c,s,n=H.unpack(h);p=sys.stdin.buffer.read(n)
 if magic!=M or v!=1 or len(p)!=n: break
 if c==1:
  time.sleep(float(os.getenv('LITE_FAKE_HELLO_DELAY','0')))
  send(101,s,b'LitePersistent/v1')
 elif c==2:
  time.sleep(float(os.getenv('LITE_FAKE_LOAD_DELAY','0')))
  init+=1
  print('[LiteAttest/v1] backend=Vulkan0; device=Vulkan0; description=NVIDIA GeForce GTX 1650',file=sys.stderr,flush=True)
  send(102,s,struct.pack('<QII',ctx,init,synth))
  time.sleep(float(os.getenv('LITE_FAKE_BEFORE_SYNTH_READ','0')))
 elif c==3:
  seed,speed=struct.unpack_from('<If',p);a=8
  text,a=field(p,a);lang,a=field(p,a);reftext,a=field(p,a);pcm,a=field(p,a)
  if text==b'CRASH': os._exit(19)
  if text==b'SLOW': time.sleep(3)
  if text.startswith(b'DELAY250'): time.sleep(.25)
  if text.startswith(b'DELAY400'): time.sleep(.4)
  if text==b'LATEHEADER': time.sleep(1)
  synth+=1;value=(sum(text)+seed+sum(pcm[:20]))%30000
  raw=struct.pack('<h',value)*2400
  body=struct.pack('<QIIII',ctx,init,synth,24000,2400)+raw
  if text==b'BADSEQ': send(103,s+1,body)
  elif text==b'OVERSIZE': sys.stdout.buffer.write(H.pack(M,1,103,s,100*1024*1024+99));sys.stdout.buffer.flush()
  elif text==b'PARTIAL':
   sys.stdout.buffer.write(H.pack(M,1,103,s,len(body))+body[:7]);sys.stdout.buffer.flush();time.sleep(3)
  else: send(103,s,body)
 elif c==4: send(104,s)
 elif c==5: send(104,s);break
 else: send(255,s)
'''

def make_wav(path: Path, value=2):
    with wave.open(str(path),"wb") as out:
        out.setnchannels(1);out.setsampwidth(2);out.setframerate(24000)
        out.writeframes(struct.pack("<h",value)*2400)

@pytest.fixture
def persistent(tmp_path,monkeypatch):
    bin_dir=tmp_path/"bin";bin_dir.mkdir();fake=bin_dir/"fake.py";fake.write_text(FAKE)
    settings=Settings(tmp_path,tmp_path,bin_dir,2)
    monkeypatch.setattr("lite.resident_worker.persistent_capability",lambda _: {"supported":True})
    monkeypatch.setattr("lite.resident_worker.preflight",lambda _: {"ready":True,"gpu":{"name":"NVIDIA GeForce GTX 1650","vendor_id":"0x10de"}})
    monkeypatch.setattr("lite.resident_worker.native_files",lambda _: (fake,bin_dir/"ggml-vulkan.dll"))
    (tmp_path/"base.gguf").write_bytes(b"base")
    (tmp_path/"codec.gguf").write_bytes(b"codec")
    monkeypatch.setattr("lite.resident_worker.model_pair",lambda _: (tmp_path/"base.gguf",tmp_path/"codec.gguf"))
    monkeypatch.setattr("lite.resident_worker.native_env",lambda _: {
        key:value for key,value in os.environ.items() if key.startswith("LITE_FAKE_")})
    monkeypatch.setattr("lite.resident_worker.persistent_command",lambda exe:[sys.executable,str(exe)])
    policy=WorkerSettings("persistent",None)
    worker=ResidentWorkerSupervisor(settings,lambda:policy)
    ref=tmp_path/"reference.wav";make_wav(ref)
    params={"profile":{"id":"voice-a","ref_text":"reference A","language":"en"},
            "reference":ref,"language":"en","seed":42,"request_id":"r1"}
    return worker,params

def test_two_requests_and_three_spans_reuse_real_process_context(persistent):
    worker,p=persistent
    async def run():
        first,one=await worker.generate(text="first",**p)
        pid=worker.process.pid;generation=worker.generation;context=worker.context_identity
        second,two=await worker.generate(text="second",**{**p,"request_id":"r2"})
        assert worker.process.pid==pid and worker.generation==generation
        assert one["worker_pid"]==two["worker_pid"]==pid
        script=parse_pause_script("one [pause 800ms] two [pause 1.5s] three")
        data,info=await worker.generate_paused(script=script,**{k:v for k,v in p.items() if k!="request_id"},request_id="r3")
        assert worker.process.pid==pid and worker.context_identity==context
        assert worker.cold_load_count==1 and worker.synthesis_count==5 and worker.reuse_count==4
        assert info["pause_frames"]==55200 and info["speech_segments"]==3
        with wave.open(__import__('io').BytesIO(data),'rb') as wav:
            raw=wav.readframes(wav.getnframes())
        assert raw[2400*2:(2400+19200)*2]==b'\0'*(19200*2)
        await worker.stop();assert worker.process is None and worker.state=="UNLOADED"
    asyncio.run(run())

def test_a_b_a_seed_and_reference_are_request_scoped(persistent,tmp_path):
    worker,p=persistent
    other=tmp_path/"other.wav";make_wav(other,99)
    async def run():
        a1,_=await worker.generate(text="same",**p)
        b,_=await worker.generate(text="same",**{**p,"reference":other,"seed":43,"request_id":"b"})
        a2,_=await worker.generate(text="same",**{**p,"request_id":"a2"})
        assert a1==a2 and a1!=b
        await worker.stop()
    asyncio.run(run())

@pytest.mark.parametrize("text,error",[("CRASH",Exception),("SLOW",Exception)])
def test_crash_or_timeout_clears_residency_without_replay(persistent,text,error):
    worker,p=persistent
    if text=="SLOW": worker.settings=Settings(worker.settings.data_dir,worker.settings.cache_dir,worker.settings.bin_dir,.5)
    async def run():
        with pytest.raises(error): await worker.generate(text=text,**p)
        assert worker.process is None and worker.state=="ERROR" and worker.synthesis_count==0
        prior_loads=worker.cold_load_count
        worker.settings=Settings(worker.settings.data_dir,worker.settings.cache_dir,worker.settings.bin_dir,2)
        data,_=await worker.generate(text="new explicit request",**p)
        assert data.startswith(b"RIFF") and worker.cold_load_count==prior_loads+1
        await worker.stop()
    asyncio.run(run())

def test_remaining_rejects_expired_absolute_deadline():
    with pytest.raises(NativeTimeout):
        remaining(0)

@pytest.mark.parametrize("operation",["persistent_capability","native_files","model_pair"])
def test_slow_cold_filesystem_verification_obeys_deadline_before_spawn(
        persistent,monkeypatch,operation):
    worker,p=persistent
    import lite.resident_worker as module
    original=getattr(module,operation)
    def slow(*args,**kwargs):
        time.sleep(.45)
        return original(*args,**kwargs)
    monkeypatch.setattr(module,operation,slow)
    spawned=[]
    monkeypatch.setattr(module,"persistent_command",lambda exe:(spawned.append(exe) or [sys.executable,str(exe)]))
    worker.settings=Settings(worker.settings.data_dir,worker.settings.cache_dir,
                             worker.settings.bin_dir,.2)
    async def run():
        with pytest.raises(NativeTimeout): await worker.generate(text="never-spawn",**p)
        assert not spawned and worker.process is None and worker.state=="ERROR"
    asyncio.run(run())

def test_expired_deadline_cancels_subprocess_creation_before_native_spawn(persistent,monkeypatch):
    worker,p=persistent
    import lite.resident_worker as module
    real_spawn=module.asyncio.create_subprocess_exec
    spawned=[]
    async def slow_spawn(*args,**kwargs):
        await asyncio.sleep(.45)
        spawned.append(args)
        return await real_spawn(*args,**kwargs)
    monkeypatch.setattr(module.asyncio,"create_subprocess_exec",slow_spawn)
    worker.settings=Settings(worker.settings.data_dir,worker.settings.cache_dir,
                             worker.settings.bin_dir,.2)
    async def run():
        with pytest.raises(NativeTimeout): await worker.generate(text="no-process",**p)
        assert not spawned and worker.process is None and worker.state=="ERROR"
    asyncio.run(run())

def test_warm_requests_reuse_cryptographically_verified_model_identity(persistent,monkeypatch):
    worker,p=persistent
    import lite.resident_worker as module
    verified=module.model_pair
    calls=[]
    def counted(settings):
        calls.append(1)
        return verified(settings)
    monkeypatch.setattr(module,"model_pair",counted)
    async def run():
        first,_=await worker.generate(text="cold",**p)
        second,_=await worker.generate(text="warm",**{**p,"request_id":"warm"})
        assert first.startswith(b"RIFF") and second.startswith(b"RIFF")
        assert len(calls)==2 and worker.cold_load_count==1 and worker.reuse_count==1
        await worker.stop()
    asyncio.run(run())

def test_invalid_model_change_forces_full_verification_and_fault(persistent,monkeypatch):
    worker,p=persistent
    import lite.resident_worker as module
    verified=module.model_pair
    base=worker.settings.data_dir/"base.gguf"
    calls=[]
    def validating(settings):
        calls.append(1)
        if base.read_bytes()!=b"base":
            raise RuntimeErrorLite("Q8 trusted SHA256 mismatch")
        return verified(settings)
    monkeypatch.setattr(module,"model_pair",validating)
    async def run():
        await worker.generate(text="cold",**p)
        base.write_bytes(b"evil")
        with pytest.raises(RuntimeErrorLite):
            await worker.generate(text="must-fail",**{**p,"request_id":"changed"})
        assert len(calls)==3 and worker.process is None and worker.state=="ERROR"
        assert worker.synthesis_count==1
    asyncio.run(run())

def test_verified_model_identity_change_invalidates_context_then_cold_starts(persistent,monkeypatch):
    worker,p=persistent
    import lite.resident_worker as module
    verified=module.model_pair
    base=worker.settings.data_dir/"base.gguf"
    calls=[]
    def counted(settings):
        calls.append(1)
        return verified(settings)
    monkeypatch.setattr(module,"model_pair",counted)
    async def run():
        await worker.generate(text="cold",**p)
        first_generation=worker.generation
        base.write_bytes(b"new!")
        with pytest.raises(NativeFailure):
            await worker.generate(text="stale-context",**{**p,"request_id":"stale"})
        assert worker.process is None and worker.synthesis_count==1 and len(calls)==3
        data,_=await worker.generate(text="new-cold",**{**p,"request_id":"new-cold"})
        assert data.startswith(b"RIFF") and worker.generation==first_generation+1 and len(calls)==5
        await worker.stop()
    asyncio.run(run())

def test_model_change_during_generation_cannot_publish(persistent,monkeypatch):
    worker,p=persistent
    import lite.resident_worker as module
    verified=module.model_pair
    base=worker.settings.data_dir/"base.gguf"
    calls=[]
    monkeypatch.setattr(module,"model_pair",lambda settings:(calls.append(1) or verified(settings)))
    async def run():
        task=asyncio.create_task(worker.generate(text="DELAY400 model",**p))
        while worker.state!="GENERATING": await asyncio.sleep(.005)
        base.write_bytes(b"swap")
        with pytest.raises(NativeFailure): await task
        assert worker.process is None and worker.state=="ERROR" and len(calls)==3
    asyncio.run(run())

def test_model_change_during_native_load_is_reverified_and_reaped(persistent,monkeypatch):
    worker,p=persistent
    base=worker.settings.data_dir/"base.gguf"
    monkeypatch.setenv("LITE_FAKE_LOAD_DELAY","0.4")
    async def run():
        task=asyncio.create_task(worker.generate(text="never-publish",**p))
        while worker.state!="LOADING": await asyncio.sleep(.005)
        base.write_bytes(b"swap")
        with pytest.raises(NativeFailure): await task
        assert worker.process is None and worker.state=="ERROR" and worker.synthesis_count==0
    asyncio.run(run())

def test_multispan_uses_one_cumulative_deadline_and_recovers_without_replay(persistent):
    worker,p=persistent
    worker.settings=Settings(worker.settings.data_dir,worker.settings.cache_dir,
                             worker.settings.bin_dir,.65)
    script=parse_pause_script("DELAY250 one [pause] DELAY250 two [pause] DELAY250 three")
    async def run():
        started=asyncio.get_running_loop().time()
        with pytest.raises(NativeTimeout):
            await worker.generate_paused(
                script=script,**{k:v for k,v in p.items() if k!="request_id"},request_id="cumulative")
        assert asyncio.get_running_loop().time()-started < 1.25
        assert worker.process is None and worker.state=="ERROR" and worker._idle_deadline is None
        prior=worker.cold_load_count
        worker.settings=Settings(worker.settings.data_dir,worker.settings.cache_dir,
                                 worker.settings.bin_dir,2)
        data,_=await worker.generate(text="new explicit",**{**p,"request_id":"after-timeout"})
        assert data.startswith(b"RIFF") and worker.cold_load_count==prior+1
        await worker.stop()
    asyncio.run(run())

@pytest.mark.parametrize("script_text",[
    "DELAY400 first [pause] DELAY400 second",
    "first [pause] second [pause] SLOW",
])
def test_second_or_final_span_timeout_reaps_and_publishes_nothing(persistent,script_text):
    worker,p=persistent
    worker.settings=Settings(worker.settings.data_dir,worker.settings.cache_dir,
                             worker.settings.bin_dir,.65)
    async def run():
        with pytest.raises(NativeTimeout):
            await worker.generate_paused(
                script=parse_pause_script(script_text),
                **{k:v for k,v in p.items() if k!="request_id"},request_id="span-timeout")
        assert worker.process is None and worker.state=="ERROR" and worker._idle_task is None
    asyncio.run(run())

def test_competing_request_rejected_until_timeout_cleanup_then_explicit_recovery(persistent):
    worker,p=persistent
    worker.settings=Settings(worker.settings.data_dir,worker.settings.cache_dir,
                             worker.settings.bin_dir,.55)
    async def run():
        first=asyncio.create_task(worker.generate(text="SLOW",**p))
        while worker.state!="GENERATING": await asyncio.sleep(.005)
        with pytest.raises(NativeBusy):
            await worker.generate(text="competitor",**{**p,"request_id":"competing"})
        with pytest.raises(NativeTimeout): await first
        assert worker.process is None and worker.state=="ERROR"
        worker.settings=Settings(worker.settings.data_dir,worker.settings.cache_dir,
                                 worker.settings.bin_dir,2)
        data,_=await worker.generate(text="recovery",**{**p,"request_id":"recovery"})
        assert data.startswith(b"RIFF")
        await worker.stop()
    asyncio.run(run())

@pytest.mark.parametrize("text",["LATEHEADER","PARTIAL"])
def test_header_and_payload_reads_share_absolute_budget(persistent,text):
    worker,p=persistent
    worker.settings=Settings(worker.settings.data_dir,worker.settings.cache_dir,
                             worker.settings.bin_dir,.4)
    async def run():
        with pytest.raises(NativeTimeout): await worker.generate(text=text,**p)
        assert worker.process is None and worker.state=="ERROR"
    asyncio.run(run())

def test_cold_hello_and_load_share_deadline(persistent,monkeypatch):
    worker,p=persistent
    monkeypatch.setenv("LITE_FAKE_HELLO_DELAY","0.25")
    monkeypatch.setenv("LITE_FAKE_LOAD_DELAY","0.30")
    worker.settings=Settings(worker.settings.data_dir,worker.settings.cache_dir,
                             worker.settings.bin_dir,.45)
    async def run():
        with pytest.raises(NativeTimeout): await worker.generate(text="never",**p)
        assert worker.process is None and worker.state=="ERROR"
    asyncio.run(run())

def test_stdin_drain_is_bounded_by_same_deadline(persistent,monkeypatch,tmp_path):
    worker,p=persistent
    monkeypatch.setenv("LITE_FAKE_BEFORE_SYNTH_READ","1")
    large=tmp_path/"large-reference.wav"
    with wave.open(str(large),"wb") as out:
        out.setnchannels(1);out.setsampwidth(2);out.setframerate(24000)
        out.writeframes(b"\x01\x00"*(60*24000))
    worker.settings=Settings(worker.settings.data_dir,worker.settings.cache_dir,
                             worker.settings.bin_dir,.45)
    async def run():
        with pytest.raises(NativeTimeout):
            await worker.generate(text="drain",**{**p,"reference":large})
        assert worker.process is None and worker.state=="ERROR"
    asyncio.run(run())

def test_cancellation_while_transmitting_reaps_before_unlock(persistent,monkeypatch,tmp_path):
    worker,p=persistent
    monkeypatch.setenv("LITE_FAKE_BEFORE_SYNTH_READ","2")
    large=tmp_path/"cancel-reference.wav"
    with wave.open(str(large),"wb") as out:
        out.setnchannels(1);out.setsampwidth(2);out.setframerate(24000)
        out.writeframes(b"\x01\x00"*(60*24000))
    async def run():
        task=asyncio.create_task(worker.generate(text="cancel-drain",**{**p,"reference":large}))
        while worker.state!="GENERATING": await asyncio.sleep(.005)
        await asyncio.sleep(.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert worker.process is None and worker.state=="ERROR" and not worker.lock.locked()
    asyncio.run(run())

def test_final_assembly_is_inside_deadline_and_faults_resident_context(persistent,monkeypatch):
    worker,p=persistent
    import lite.resident_worker as module
    original=module.assemble_pause_wav
    def late_assembly(*args,**kwargs):
        __import__("time").sleep(.7)
        return original(*args,**kwargs)
    monkeypatch.setattr(module,"assemble_pause_wav",late_assembly)
    worker.settings=Settings(worker.settings.data_dir,worker.settings.cache_dir,
                             worker.settings.bin_dir,.5)
    async def run():
        with pytest.raises(NativeTimeout):
            await worker.generate_paused(
                script=parse_pause_script("first [pause] second"),
                **{k:v for k,v in p.items() if k!="request_id"},request_id="late-assembly")
        assert worker.process is None and worker.state=="ERROR" and worker._idle_task is None
    asyncio.run(run())

def test_final_reference_integrity_is_inside_operation_and_fail_closed(persistent):
    worker,p=persistent
    async def run():
        task=asyncio.create_task(worker.generate(text="DELAY400 integrity",**p))
        while worker.state!="GENERATING": await asyncio.sleep(.005)
        make_wav(p["reference"],99)
        with pytest.raises(NativeFailure): await task
        assert worker.process is None and worker.state=="ERROR"
    asyncio.run(run())

def test_atomic_settings_all_policies_and_invalid_values(tmp_path):
    store=WorkerSettingsStore(tmp_path)
    assert store.load()==WorkerSettings("legacy_oneshot",600)
    for timeout in ALLOWED_IDLE_TIMEOUTS:
        expected=WorkerSettings("persistent",timeout)
        assert store.save(expected)==expected and store.load()==expected
        assert json.loads(store.path.read_text())["schema"]=="lite-worker-settings-v1"
    with pytest.raises(WorkerSettingsError): store.save(WorkerSettings("automatic",600))
    with pytest.raises(WorkerSettingsError): store.save(WorkerSettings("persistent",59))

@pytest.mark.parametrize("timeout",[60,300,600,1800,3600])
def test_idle_arms_only_after_completed_operation(persistent,monkeypatch,timeout):
    worker,p=persistent
    worker.settings_provider=lambda:WorkerSettings("persistent",timeout)
    async def run():
        task=asyncio.create_task(worker.generate(text="first",**p))
        while worker.state not in {"LOADING","GENERATING","READY"}: await asyncio.sleep(0)
        assert worker._idle_deadline is None
        await task
        assert worker._idle_deadline is not None and worker.status()["idle_remaining_seconds"]<=timeout
        await worker.stop()
    asyncio.run(run())

def test_never_does_not_arm_idle(persistent):
    worker,p=persistent
    async def run():
        await worker.generate(text="first",**p)
        assert worker._idle_deadline is None
        await worker.stop()
    asyncio.run(run())

@pytest.mark.parametrize("text",["BADSEQ","OVERSIZE"])
def test_mismatched_or_oversize_reply_is_rejected_and_reaped(persistent,text):
    worker,p=persistent
    async def run():
        with pytest.raises(NativeFailure): await worker.generate(text=text,**p)
        assert worker.process is None and worker.state=="ERROR"
    asyncio.run(run())

def test_cancellation_and_worker_death_between_spans_publish_nothing(persistent):
    worker,p=persistent
    async def run():
        slow=asyncio.create_task(worker.generate(text="SLOW",**p))
        while worker.state!="GENERATING": await asyncio.sleep(.01)
        slow.cancel()
        with pytest.raises(asyncio.CancelledError): await slow
        assert worker.process is None and worker.state=="ERROR"
        script=parse_pause_script("first [pause] CRASH [pause] never")
        with pytest.raises(NativeFailure):
            await worker.generate_paused(script=script,**{k:v for k,v in p.items() if k!="request_id"},request_id="dead")
        assert worker.process is None and worker.synthesis_count==1
    asyncio.run(run())

def test_idle_generation_token_prevents_old_timer_unload_race(persistent):
    worker,p=persistent
    # Keep the boundaries comfortably outside the Windows scheduler quantum.
    # This still exercises an old timer firing after a newer request, without
    # turning runner contention into a false failure.
    worker.settings_provider=lambda:WorkerSettings("persistent",.6)
    async def run():
        await worker.generate(text="first",**p);generation=worker.generation;pid=worker.process.pid
        await asyncio.sleep(.1)
        await worker.generate(text="second",**{**p,"request_id":"second"})
        await asyncio.sleep(.2)
        assert worker.state=="READY" and worker.generation==generation and worker.process.pid==pid
        await asyncio.sleep(.45)
        for _ in range(200):
            if worker.state=="UNLOADED": break
            await asyncio.sleep(.01)
        assert worker.state=="UNLOADED" and worker.process is None
    asyncio.run(run())

def test_manual_unload_then_explicit_request_cold_starts_new_native_counter(persistent):
    worker,p=persistent
    async def run():
        await worker.generate(text="before unload",**p);first_generation=worker.generation
        await worker.unload_now();assert worker.state=="UNLOADED"
        data,_=await worker.generate(text="after unload",**{**p,"request_id":"after"})
        assert data.startswith(b"RIFF") and worker.generation==first_generation+1
        assert worker.cold_load_count==2 and worker.synthesis_count==2 and worker.reuse_count==0
        await worker.stop()
    asyncio.run(run())

def test_worker_http_settings_status_restart_and_busy(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from lite.app import create_app
    from lite.native_runner import Runner
    from lite.profiles import Store
    root=tmp_path/"data";root.mkdir();bin_dir=tmp_path/"bin";bin_dir.mkdir()
    settings=Settings(root,tmp_path,bin_dir,30)
    monkeypatch.setattr("lite.app.persistent_capability",lambda _: {"supported":True,"marker":"LitePersistent/v1","protocol_version":1})
    app=create_app(settings,Store(settings),runner=Runner(settings))
    with TestClient(app) as client:
        assert client.get("/lite/worker/settings").json()["idle_timeout_seconds"]==600
        bad=client.put("/lite/worker/settings",json={"mode":"persistent","idle_timeout_seconds":61})
        assert bad.status_code==422 and bad.json()["error"]["code"]=="INVALID_WORKER_SETTINGS"
        saved=client.put("/lite/worker/settings",json={"mode":"persistent","idle_timeout_seconds":None})
        assert saved.status_code==200 and saved.json()["mode"]=="persistent"
        status=client.get("/lite/worker/status").json()
        assert status["state"]=="UNLOADED" and status["resident"] is False
        assert client.post("/lite/worker/unload").status_code==200
    restarted=create_app(settings,Store(settings),runner=Runner(settings))
    with TestClient(restarted) as client:
        assert client.get("/lite/worker/settings").json()["mode"]=="persistent"
        assert client.get("/lite/worker/status").json()["state"]=="UNLOADED"
        class Busy:
            def locked(self): return True
        restarted.state.runner.lock=Busy()
        response=client.post("/lite/worker/unload")
        assert response.status_code==409 and response.json()["error"]["code"]=="WORKER_BUSY"

def test_capability_receipt_binds_exe_pin_and_patch_sources(tmp_path,monkeypatch):
    import hashlib
    from lite.config import ROOT,RUNTIME_REVISION
    from lite import resident_worker as module
    bin_dir=tmp_path/"bin";bin_dir.mkdir();exe=bin_dir/"omnivoice-tts-windows-x86_64.exe";exe.write_bytes(b"MZ"+b"worker"*50)
    (bin_dir/"checksums.sha256").write_text("fixture")
    settings=Settings(tmp_path,tmp_path,bin_dir,30)
    monkeypatch.setattr(module,"native_files",lambda _:(exe,bin_dir/"ggml-vulkan.dll"))
    sha=lambda path:hashlib.sha256(path.read_bytes()).hexdigest()
    receipt={"schema":"lite-persistent-v1","marker":"LitePersistent/v1","protocol_version":1,
             "runtime_revision":RUNTIME_REVISION,"exe_sha256":sha(exe),
             "patch_sha256":sha(ROOT/"scripts/internal/stage-persistent-worker.py"),
             "source_sha256":sha(ROOT/"scripts/internal/lite-persistent-worker.cpp")}
    path=bin_dir/"persistent-capability.json";path.write_text(json.dumps(receipt))
    module._CAPABILITY_CACHE.clear();assert module.persistent_capability(settings)["supported"] is True
    path.write_text(json.dumps({**receipt,"source_sha256":"0"*64}))
    module._CAPABILITY_CACHE.clear();assert module.persistent_capability(settings)["supported"] is False
