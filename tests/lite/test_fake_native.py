"""Fake native executable validates actual stdin, argv, WAV publication and cleanup."""
import asyncio
import sys
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
p=argparse.ArgumentParser()
p.add_argument("--model");p.add_argument("--codec");p.add_argument("-o")
p.add_argument("--ref-wav");p.add_argument("--ref-text")
p.add_argument("--lang",default="English")
p.add_argument("--seed",default="0")
args=p.parse_args()
data=sys.stdin.buffer.read()
if not data:
    sys.exit(2)
if b"slow" in data:
    time.sleep(4)
if b"failed" in data:
    sys.exit(9)
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
    path.chmod(0o755)
    settings=Settings(data_dir=tmp_path,cache_dir=tmp_path,bin_dir=bin_dir,timeout=0.1)
    monkeypatch.setattr("lite.native_runner.preflight",lambda settings: {"ready":True})
    monkeypatch.setattr("lite.native_runner.native_files",lambda settings: (path,bin_dir/"ggml-vulkan.dll"))
    monkeypatch.setattr("lite.native_runner.model_pair",lambda settings: (tmp_path/"base.gguf",tmp_path/"codec.gguf"))
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
        await asyncio.sleep(0.05)
        with pytest.raises(NativeBusy):
            await runner.generate(**_params(root,"hello"))
        await runner.stop()
        with pytest.raises((NativeFailure,Exception)):
            await first
    asyncio.run(execute())
    assert not list(root.glob("lite-*"))
