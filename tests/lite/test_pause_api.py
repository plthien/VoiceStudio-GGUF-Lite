"""ASGI -> real fake-native processes -> attested atomic pause WAV orchestration."""
import asyncio
import io
import os
import sys
import time
import wave
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from lite.app import create_app
from lite.config import Settings
from lite.native_runner import Runner
from lite.audio import wav_info

NATIVE = r'''
import argparse
import sys
import time
import wave
import os
from pathlib import Path
p=argparse.ArgumentParser()
for flag in ("--model","--codec","--ref-wav","--ref-text","--lang","--seed","--speed"):
    p.add_argument(flag,default=None)
p.add_argument("-o")
args=p.parse_args()
text=sys.stdin.buffer.read().decode("utf-8")
if os.environ.get("PAUSE_AUDIT"):
    with open(os.environ["PAUSE_AUDIT"],"a",encoding="utf8") as f:
        f.write(str(args.seed)+"|"+str(args.speed)+"|"+text+"|"+Path(args.ref_text).read_text(encoding="utf8")+"\n")
if "slow" in text:
    time.sleep(4)
if "bad-second" in text:
    sys.exit(17)
device="AMD" if "bad-proof" in text else "NVIDIA GeForce GTX 1650"
print("[LiteAttest/v1] backend=Vulkan0; device=Vulkan0; description="+device,file=sys.stderr)
with wave.open(args.o,"wb") as w:
    w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000)
    w.writeframes(b"\x01\x02"*2400)
'''
@pytest.fixture
def system(tmp_path,monkeypatch):
    bin=tmp_path/"bin";bin.mkdir()
    native=bin/"synthetic.py";native.write_text(NATIVE)
    settings=Settings(data_dir=tmp_path,cache_dir=tmp_path,bin_dir=bin,timeout=20)
    import lite.native_runner as nr
    monkeypatch.setattr(nr,"preflight",lambda _:{"ready":True,"gpu":{"name":"NVIDIA GTX 1650","vendor_id":"0x10de"}})
    monkeypatch.setattr(nr,"native_files",lambda _: (native,bin/"ggml-vulkan.dll"))
    pairs=[]
    def modelpair(_):
        pairs.append(1)
        return tmp_path/"base.gguf",tmp_path/"tokenizer.gguf"
    monkeypatch.setattr(nr,"model_pair",modelpair)
    monkeypatch.setattr(nr,"native_command",lambda exe:[sys.executable,str(exe)])
    env=os.environ.copy();env["PAUSE_AUDIT"]=str(tmp_path/"audit.txt")
    monkeypatch.setattr(nr,"native_env",lambda _:env)
    monkeypatch.setattr(nr,"native_speed_capability",lambda _:{"supported":True})
    import lite.app as appmod
    monkeypatch.setattr(appmod,"native_speed_capability",lambda _:{"supported":True})
    runner=Runner(settings)
    app=create_app(settings,runner=runner)
    row={"id":"owned-voice","ref_text":"EXACT OWNED REFERENCE","language":"en","version":1}
    ref=tmp_path/"owned-ref.wav";ref.write_bytes(b"synthetic")
    app.state.store.get=lambda _:row
    app.state.store.reference=lambda _:ref
    app.state.licence.is_ready=lambda:True
    return app,runner,settings,pairs,tmp_path

def body(text,speed=1.0,seed=42):
    return {"model":"omnivoice-gguf","voice":"owned-voice","input":text,
            "speed":speed,"seed":seed,"response_format":"wav"}

def test_api_exact_pause_timing_seed_and_preflight_once(system,monkeypatch):
    app,runner,settings,pairs,root=system
    import lite.native_runner as nr
    preflights=[]
    original=nr.preflight
    monkeypatch.setattr(nr,"preflight",lambda s:(preflights.append(1),original(s))[1])
    data=body("Alpha. [pause 800ms] Bravo! [pause 1.5s] Charlie.",1.15)
    with TestClient(app) as c:
        first=c.post("/v1/audio/speech",json=data)
        assert first.status_code==200,first.text
        second=c.post("/v1/audio/speech",json=data)
        assert second.status_code==200
    assert first.content==second.content
    assert int(first.headers["x-lite-pause-count"])==2
    assert first.headers["x-lite-pause-s"]=="2.3"
    assert first.headers["x-lite-speech-segments"]=="3"
    assert first.headers["x-lite-verification-status"]=="verified"
    assert first.headers["x-lite-verified-backend"]=="Vulkan0"
    assert wav_info(first.content)["frames"]==3*2400+2300*24
    assert float(first.headers["x-lite-audio-s"])==pytest.approx(float(first.headers["x-lite-speech-s"])+2.3,abs=1/24000)
    assert len(preflights)==2,preflights  # one per whole request, not per span
    assert len(pairs)==4 # one initial, one final model hash per request
    audit=(root/"audit.txt").read_text().splitlines()
    assert len(audit)==6
    assert [x.split("|")[0] for x in audit[:3]]==["42","43","44"]
    assert [x.split("|")[2] for x in audit[:3]]==["Alpha.","Bravo!","Charlie."]
    assert all(x.split("|")[3]=="EXACT OWNED REFERENCE" for x in audit)
    assert all(x.split("|")[1]!="None" for x in audit)

@pytest.mark.parametrize("tag,code",[
    ("[pause -1s]","INVALID_PAUSE_MARKER"),
    ("[pause 1minute]","INVALID_PAUSE_MARKER"),
    ("[pause 1s","INVALID_PAUSE_MARKER"),
    ("[pause]"*17,"PAUSE_LIMIT_EXCEEDED"),
    ("[pause 10s]"*7,"PAUSE_LIMIT_EXCEEDED"),
    ("[pause]","INVALID_REQUEST"),
])
def test_bad_marker_typed_422_before_native(system,tag,code):
    app,runner,settings,pairs,root=system
    input=tag if code=="INVALID_REQUEST" else "A "+tag+" B"
    with TestClient(app) as c:
        r=c.post("/v1/audio/speech",json=body(input))
    assert r.status_code==422 and r.json()["error"]["code"]==code
    assert not (root/"audit.txt").exists()

def test_no_marker_unchanged_one_native_path_and_headers(system,monkeypatch):
    app,runner,settings,pairs,root=system
    original=runner.generate
    calls=[]
    async def spy(**kw):
        calls.append(kw.copy())
        return await original(**kw)
    runner.generate=spy
    with TestClient(app) as c:
        response=c.post("/v1/audio/speech",json=body("Untagged normal words"))
    assert response.status_code==200
    assert len(calls)==1 and calls[0]["text"]=="Untagged normal words"
    assert "speed" not in calls[0]
    assert "x-lite-pause-count" not in response.headers
    assert wav_info(response.content)["frames"]==2400
    assert len((root/"audit.txt").read_text().splitlines())==1

@pytest.mark.parametrize("text,status",[
    ("A[pause]bad-second",500),
    ("A[pause]bad-proof",500),
    ("A[pause]slow",504)
])
def test_second_span_failure_never_publishes_partial_wav(system,text,status):
    app,runner,settings,pairs,root=system
    if "slow" in text:
        runner.settings=Settings(data_dir=root,cache_dir=root,bin_dir=settings.bin_dir,timeout=.5)
    with TestClient(app) as c:
        r=c.post("/v1/audio/speech",json=body(text))
    assert r.status_code==status
    assert r.headers.get("content-type","").startswith("application/json")
    assert not runner.lock.locked() and runner.process is None
    assert not list(root.glob("lite-pause-*"))

def test_busy_admission_covers_between_spans(system,monkeypatch):
    app,runner,settings,pairs,root=system
    orig=runner._paused_span
    entered=asyncio.Event()
    release=asyncio.Event()
    call=[0]
    async def fenced(**kwargs):
        call[0]+=1
        if call[0]==2:
            entered.set()
            await release.wait()
        return await orig(**kwargs)
    runner._paused_span=fenced
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://127.0.0.1:3900") as client:
            first=asyncio.create_task(client.post("/v1/audio/speech",json=body("A[pause]B")))
            await asyncio.wait_for(entered.wait(),timeout=8)
            assert runner.lock.locked() and app.state.admission.locked()
            assert (await client.post("/v1/audio/speech",json=body("Second request"))).status_code==429
            assert (await client.delete("/profiles/owned-voice")).status_code==409
            assert (await client.get("/lite/status")).json()["busy"] is True
            release.set()
            result=await first
            assert result.status_code==200,result.text
            assert not runner.lock.locked()
    asyncio.run(exercise())
