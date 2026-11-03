"""No CUDA/model download: deterministic fake resident worker and real API/FFmpeg."""
import asyncio
import base64
import hashlib
import io
import json
import math
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lite.chatterbox_app import create_app
from lite.chatterbox_config import ChatterboxSettings, MODEL_REVISION, IDLE_CHOICES, diagnostic
from lite.chatterbox_service import TurboService, TurboError
from lite.chatterbox_ipc import pack, unpack, ProtocolError
from lite.chatterbox_audio import concatenate, stretch_speech
from lite.pause import parse_pause_script, PauseError
from lite.audio import wav_info


def audio(seconds=0.25):
    out=io.BytesIO()
    with wave.open(out,"wb") as f:
        f.setnchannels(1);f.setsampwidth(2);f.setframerate(24000)
        f.writeframes(b"\x00\x10"*int(seconds*24000))
    return out.getvalue()


class FakeTurbo(TurboService):
    def __init__(self, settings):
        super().__init__(settings)
        self.cache=set()
        self.history=[]
        self.fail=False
        self.delaying=False

    async def _ensure_loaded(self, deadline):
        self._cancel_idle()
        if self.state=="READY":
            return
        self.state="READY"
        self.loads+=1
        self.device={"name":"FAKE CUDA TEST ONLY","index":0}

    async def _rpc(self, msg, deadline):
        if self.delaying:
            await asyncio.sleep(1)
        if self.fail:
            raise TurboError("WORKER_FAILURE")
        key=(msg["voice"],msg["version"],msg["reference_sha"],MODEL_REVISION)
        if key in self.cache:self.hits+=1
        else:self.cache.add(key)
        self.history.append(key)
        self.syntheses+=1
        return {"wav":base64.b64encode(audio()).decode(),"syntheses":self.syntheses,
                "cache_hits":self.hits}

    async def invalidate(self, voice=None):
        self.cache={key for key in self.cache if voice is not None and key[0]!=voice}

    async def _kill(self):
        self._cancel_idle()
        self.cache.clear()
        self.state="UNLOADED"
        self.active_id=None
        self.device=None


@pytest.fixture
def api(tmp_path):
    settings=ChatterboxSettings(root=tmp_path,timeout=0.7)
    svc=FakeTurbo(settings)
    application=create_app(settings=settings,service=svc)
    with TestClient(application) as client:
        yield client,svc,settings


def profile(client,name="Alpha"):
    files={"ref_audio":("owned.wav",audio(6), "audio/wav")}
    r=client.post("/profiles",data={"name":name,"ref_text":"Owner spoken reference words.",
                                   "language":"en","consent":"confirmed"},files=files)
    assert r.status_code==200,r.text
    return r.json()


def gen(client,id,text="A sentence spoken aloud.",speed=1.0):
    return client.post("/v1/audio/speech",json={"model":"chatterbox-turbo","voice":id,
        "input":text,"language":"en","speed":speed,"response_format":"wav","seed":42})


def test_isolated_no_gguf(api):
    client,svc,config=api
    r=client.get("/chatterbox/readiness")
    assert r.status_code==200
    assert r.json()["installed"] is False
    assert "ENV_NOT_INSTALLED" in r.json()["issues"]
    assert client.get("/chatterbox/capability").json()["speed_mode"]=="postprocess_atempo"
    assert client.get("/health").json()["ready"] is False


def test_ipc_bounded():
    assert unpack(pack({"cmd":"STATUS"})[4:])["cmd"]=="STATUS"
    for raw in (b"{}",b'{"v":999}'):
        with pytest.raises(ProtocolError):unpack(raw)
    with pytest.raises(ProtocolError):pack({"text":"x"*(121*1024*1024)})


@pytest.mark.parametrize("invalid",["bad","", "[pause 0ms] x", "[pause 10.001s] x", "[pause zz] x"])
def test_pause_rejects(invalid):
    if invalid.startswith("[pause"):
        with pytest.raises(PauseError):parse_pause_script(invalid)


@pytest.mark.parametrize("input",["[pause -1ms]", "[pause 11s] Hi", "[pause 1.5555s] Hi", "[pause x] Hi",
                                 "Hi [pause", "Hi [pause 100ms] [pause 99999ms]"])
def test_bad_pause_api(api,input):
    c,_,_=api
    p=profile(c)
    r=gen(c,p["id"],input)
    assert r.status_code==422,r.text


@pytest.mark.parametrize("speed", [0.75,0.85,1.0,1.15,1.25])
def test_exact_pause_across_speeds(api,speed):
    c,svc,_=api
    p=profile(c)
    r=gen(c,p["id"],"Hello. [pause 250ms] Goodbye. [pause 1.5s] Again.",speed)
    assert r.status_code==200,r.text
    info=wav_info(r.content)
    assert int(float(r.headers["X-Lite-Pause-S"])*24000)==42000
    assert int(float(r.headers["X-Lite-Speech-S"])*24000)+42000==info["frames"]
    assert r.headers["X-TTS-Backend"]=="CUDA0"
    assert r.headers["X-TTS-Speed-Mode"]=="postprocess_atempo"
    assert r.headers["X-TTS-Pause-Unaffected"]=="true"
    assert r.headers["X-Lite-Pause-Count"]=="2"
    assert float(r.headers["X-TTS-Speech-WPM"])>float(r.headers["X-TTS-Final-WPM"])
    assert len(svc.history)==3


def test_speed_duration_and_bypass():
    original=audio(1)
    assert stretch_speech(original,1,10,300000) == original
    slow=stretch_speech(original,0.75,10,300000)
    fast=stretch_speech(original,1.25,10,300000)
    assert wav_info(slow)["seconds"]>1.2
    assert wav_info(fast)["seconds"]<0.85


def test_voice_cache_isolation_and_update(api):
    c,svc,_=api
    a=profile(c,"A");b=profile(c,"B")
    for id in (a["id"],b["id"],a["id"]):
        assert gen(c,id).status_code==200
    assert svc.hits>=1
    assert svc.history[0][0]==svc.history[2][0]!=svc.history[1][0]
    r=c.put("/profiles/"+a["id"]+"/audio",data={"ref_text":"Changed owned transcript",
        "consent":"confirmed"},files={"ref_audio":("new.wav",audio(6.1),"audio/wav")})
    assert r.status_code==200,r.text
    assert r.json()["version"]==2
    assert gen(c,a["id"]).status_code==200
    assert svc.history[-1][1]==2
    assert c.delete("/profiles/"+b["id"]+"?purge=true").status_code==200
    assert c.get("/profiles/"+b["id"]).status_code==404


def test_invalid_consent_reference(api):
    c,_,_=api
    r=c.post("/profiles",data={"name":"x","ref_text":"exact","consent":"not-confirmed"},
       files={"ref_audio":("a.wav",audio(6),"audio/wav")})
    assert r.status_code==422
    r=c.post("/profiles",data={"name":"x","ref_text":"exact","consent":"confirmed"},
       files={"ref_audio":("a.wav",audio(5),"audio/wav")})
    assert r.status_code==422 and r.json()["error"]["code"]=="REFERENCE_TOO_SHORT"


def test_model_and_validation(api):
    c,_,_=api;p=profile(c)
    for override in ({"model":"omnivoice-gguf"},{"speed":1.3},{"language":"vi"},
                      {"response_format":"mp3"},{"input":" "},{"seed":-1}):
        body={"model":"chatterbox-turbo","voice":p["id"],"input":"Hello"}
        body.update(override)
        r=c.post("/v1/audio/speech",json=body)
        assert r.status_code==422,r.text
    assert gen(c,"unknown").status_code==404


def test_failure_never_returns_partial_wav(api):
    c,svc,_=api;p=profile(c)
    svc.fail=True
    r=gen(c,p["id"],"Hello [pause 250ms] Next")
    assert r.status_code==500
    assert not r.content.startswith(b"RIFF")
    assert svc.state=="ERROR"


def test_unload_idle_settings(api):
    c,svc,_=api;p=profile(c)
    assert gen(c,p["id"]).status_code==200
    assert svc.status()["load_count"]==1
    for timeout in IDLE_CHOICES:
        r=c.put("/chatterbox/runtime/settings",json={"idle_timeout_seconds":timeout})
        assert r.status_code==200
        assert c.get("/chatterbox/runtime/settings").json()["idle_timeout_seconds"]==timeout
    assert c.post("/chatterbox/runtime/unload").status_code==200
    assert svc.state=="UNLOADED"
    assert gen(c,p["id"]).status_code==200
    assert svc.loads==2


def test_security_host_origin_and_upload_limit(api):
    c,_,_=api
    assert c.get("/health",headers={"host":"attacker.invalid"}).status_code==403
    assert c.post("/chatterbox/runtime/unload",headers={"origin":"http://evil.invalid"}).status_code==403
    assert c.post("/profiles",headers={"content-length":"999999999"}).status_code==413
    assert c.get("/ui/assets/evil.js").status_code==404


def test_ui_assets_and_labels(api):
    c,_,_=api
    html=c.get("/ui").text
    assert "Voice Cloning" in html and "Runtime &amp; Settings" in html
    assert "post-processing" in html
    script=c.get("/ui/assets/app.js")
    assert script.status_code==200
    assert "fetch(" in script.text


def test_longform_resume_provenance(tmp_path):
    from lite.chatterbox_assembly import section_key,atomic
    p={"input":"Hello", "speed":1}
    assert section_key(p,"id",1) != section_key(p,"id",2)
    assert section_key(p,"id",1) != section_key({"input":"Different","speed":1},"id",1)
    target=tmp_path/"private"/"segment.wav"
    atomic(target,audio())
    assert wav_info(target.read_bytes())["frames"]==6000


def test_concat_canonical():
    r=concatenate([audio(.1),audio(.2)])
    assert wav_info(r)["frames"]==7200



def test_cpu_diagnostic_does_not_import_torch():
    import sys
    import lite.chatterbox_cpu_diagnostic as diagnostic
    assert callable(diagnostic.cpu_diagnostic)
    assert "torch" not in sys.modules



def test_atempo_preserves_approximate_pitch():
    import math
    import struct
    out = io.BytesIO()
    count = 24000
    pcm = b"".join(struct.pack("<h",int(9000*math.sin(2*math.pi*440*i/24000)))
                   for i in range(count))
    with wave.open(out,"wb") as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000)
        w.writeframes(pcm)
    for speed in (0.75,1.25):
        stretched=stretch_speech(out.getvalue(),speed,10,300000)
        frames=wav_info(stretched)["frames"]
        with wave.open(io.BytesIO(stretched),"rb") as w:
            samples=struct.unpack("<"+"h"*frames,w.readframes(frames))
        crossings=sum(a<=0<b for a,b in zip(samples,samples[1:]))
        pitch=crossings/(frames/24000)
        assert abs(pitch-440)<20,(speed,pitch)
