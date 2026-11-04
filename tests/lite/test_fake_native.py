"""Fake native executable validates actual stdin, argv, WAV publication and cleanup."""
import asyncio
import csv
import os
import subprocess
import sys
import time
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from lite.config import Settings
from lite.native_runner import Runner, NativeBusy, NativeTimeout, NativeFailure

FAKE = r'''#!/usr/bin/env python3
import argparse
import sys
import time
import wave
import subprocess
from pathlib import Path
p=argparse.ArgumentParser()
p.add_argument("--model");p.add_argument("--codec");p.add_argument("-o")
p.add_argument("--ref-wav");p.add_argument("--ref-text")
p.add_argument("--lang",default="English")
p.add_argument("--seed",default="0")
p.add_argument("--speed",default=None)
args=p.parse_args()
data=sys.stdin.buffer.read()
if not data:
    sys.exit(2)
if b"slow" in data:
    time.sleep(4)
if b"failed" in data:
    sys.exit(9)
if b"child" in data:
    kid=subprocess.Popen([sys.executable,"-c","import time;time.sleep(120)"],
                         stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    Path(args.ref_wav).with_suffix(".childpid").write_text(str(kid.pid))
    time.sleep(120)
if b"noisy" in data:
    sys.stderr.buffer.write(b"x"*150000)
    sys.stderr.flush()
    time.sleep(1)
print("[LiteAttest/v1] backend=Vulkan0; device=Vulkan0; description=NVIDIA GeForce GTX 1650",file=sys.stderr)
with wave.open(args.o, "wb") as out:
    out.setnchannels(1)
    out.setsampwidth(2)
    out.setframerate(24000)
    out.writeframes(b"\x01\x00"*2400)
'''

@pytest.fixture
def fixture(tmp_path, monkeypatch):
    bin_dir=tmp_path/"bin"
    bin_dir.mkdir()
    path=bin_dir/"fake-native"
    path.write_text(FAKE)
    # Explicit Python interpreter is used on both Windows and Linux.
    # Windows process creation can exceed 100 ms under CI load; the synthetic
    # slow path sleeps four seconds, so one second remains a deterministic bound.
    settings=Settings(data_dir=tmp_path,cache_dir=tmp_path,bin_dir=bin_dir,timeout=1.0)
    monkeypatch.setattr("lite.native_runner.preflight",lambda settings: {"ready":True,"gpu":{"name":"NVIDIA GeForce GTX 1650","vendor_id":"0x10de"}})
    monkeypatch.setattr("lite.native_runner.native_files",lambda settings: (path,bin_dir/"ggml-vulkan.dll"))
    monkeypatch.setattr("lite.native_runner.model_pair",lambda settings: (tmp_path/"base.gguf",tmp_path/"codec.gguf"))
    monkeypatch.setattr("lite.native_runner.native_command",lambda exe: [sys.executable,str(exe)])
    return Runner(settings), tmp_path

def _params(root, text):
    ref=root/"reference.wav"
    ref.write_bytes(b"fake")
    return dict(profile={"id":"voice-test","ref_text":"Exact reference words","language":"en"},
                reference=ref,text=text,language="en",seed=42,request_id="request-123")

def test_real_subprocess_success_and_workspace_cleanup(fixture):
    runner,root=fixture
    # Timeout is intentionally tiny for slow-path testing; fast path is cheap.
    runner.settings=Settings(data_dir=root,cache_dir=root,bin_dir=runner.settings.bin_dir,timeout=20)
    async def execute():
        data,info=await runner.generate(**_params(root,"hello world"))
        assert data[:4]==b"RIFF"
        assert info["audio_s"]==0.1
        assert info["rtf"]>0
        assert info["profile_id"]=="voice-test"
        assert runner.active_id is None
        assert runner.process is None
    asyncio.run(execute())
    assert not list(root.glob("lite-*"))

def test_nonzero_exit_never_emits_wav(fixture):
    runner,root=fixture
    async def execute():
        with pytest.raises(NativeFailure):
            await runner.generate(**_params(root,"failed"))
    asyncio.run(execute())
    assert not list(root.glob("lite-*"))

def test_timeout_kills_owned_subprocess_and_cleans_temp(fixture):
    runner,root=fixture
    async def execute():
        with pytest.raises(NativeTimeout):
            await runner.generate(**_params(root,"slow"))
        assert runner.process is None
        assert runner.active_id is None
    asyncio.run(execute())
    assert not list(root.glob("lite-*"))

def test_concurrent_native_busy_rejects_without_queue(fixture):
    runner,root=fixture
    runner.settings=Settings(data_dir=root,cache_dir=root,bin_dir=runner.settings.bin_dir,timeout=10)
    async def execute():
        first=asyncio.create_task(runner.generate(**_params(root,"slow")))
        for _ in range(200):
            if runner.process is not None and runner.process.returncode is None:
                break
            await asyncio.sleep(0.01)
        assert runner.process is not None and runner.process.returncode is None
        with pytest.raises(NativeBusy):
            await runner.generate(**_params(root,"hello"))
        await runner.stop()
        with pytest.raises(NativeFailure):
            await first
        assert runner.process is None and runner.active_id is None
    asyncio.run(execute())
    assert not list(root.glob("lite-*"))

def test_large_native_stderr_never_publishes_wav(fixture):
    runner,root=fixture
    runner.settings=Settings(data_dir=root,cache_dir=root,bin_dir=runner.settings.bin_dir,timeout=8)
    async def execute():
        with pytest.raises(NativeFailure):
            await runner.generate(**_params(root,"noisy"))
        assert runner.process is None and runner.active_id is None
    asyncio.run(execute())
    assert not list(root.glob("lite-*"))

def _active_pid(pid):
    if os.name=="nt":
        proc=subprocess.run(["tasklist","/FI","PID eq "+str(pid),"/FO","CSV","/NH"],
                            capture_output=True,text=True,timeout=8)
        for row in csv.reader(proc.stdout.splitlines()):
            if len(row)>1 and row[1]==str(pid):
                return True
        return False
    stat=Path("/proc")/str(pid)/"stat"
    if stat.exists() and stat.read_text().split(") ")[1].startswith("Z"):
        return False
    try:
        os.kill(pid,0)
        return True
    except ProcessLookupError:
        return False

def test_stop_reaps_child_process_tree_and_releases_slot(fixture):
    runner,root=fixture
    runner.settings=Settings(data_dir=root,cache_dir=root,bin_dir=runner.settings.bin_dir,timeout=15)
    async def execute():
        first=asyncio.create_task(runner.generate(**_params(root,"child")))
        pid_path=root/"reference.childpid"
        for _ in range(100):
            if pid_path.exists():
                break
            await asyncio.sleep(0.05)
        assert pid_path.exists(),"Native fake did not spawn child process"
        child=int(pid_path.read_text())
        assert _active_pid(child)
        await runner.stop()
        with pytest.raises(NativeFailure):
            await first
        for _ in range(60):
            if not _active_pid(child):
                break
            await asyncio.sleep(0.05)
        assert not _active_pid(child),"Owned native child remained after stop"
        assert runner.process is None and runner.active_id is None
    asyncio.run(execute())
    assert not list(root.glob("lite-*"))

@pytest.mark.parametrize("requested",[0.85,1.004,1.127,1.15,1.234,0.999,1.25])
def test_real_fake_native_argv_float32_round_trip(fixture,monkeypatch,requested):
    from lite.speed import speed_to_native
    import lite.native_runner as mod
    runner,root=fixture
    runner.settings=Settings(data_dir=root,cache_dir=root,bin_dir=runner.settings.bin_dir,timeout=20)
    monkeypatch.setattr(mod,"native_speed_capability",lambda _:{"supported":True})
    original=asyncio.create_subprocess_exec
    seen=[]
    async def spy(*args,**kwargs):
        seen.append(args)
        return await original(*args,**kwargs)
    monkeypatch.setattr(asyncio,"create_subprocess_exec",spy)
    async def test():
        data,info=await runner.generate(**_params(root,"Precision native argv"),speed=requested)
        assert data.startswith(b"RIFF")
    asyncio.run(test())
    effective,arg=speed_to_native(requested)
    assert seen and "--speed" in seen[0]
    assert seen[0][seen[0].index("--speed")+1]==arg
    import struct
    assert struct.unpack("<f",struct.pack("<f",float(arg)))[0]==effective
    if requested==1.004: assert arg!="1.00"
