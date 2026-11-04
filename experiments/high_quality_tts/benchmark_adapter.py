"""Isolated high-quality-engine comparison adapter; no production imports."""
from __future__ import annotations
import importlib.util, io, time, wave, audioop
from dataclasses import dataclass

@dataclass(frozen=True)
class Probe:
    backend:str
    available:bool
    status:str

def probe(backend:str, *, licence_accepted:bool=False)->Probe:
    if backend=="fake":return Probe(backend,True,"READY")
    if backend=="pocket_tts":
        installed=importlib.util.find_spec("pocket_tts") is not None
        if not installed:return Probe(backend,False,"RUNTIME_UNAVAILABLE")
        if not licence_accepted:return Probe(backend,False,"LICENCE_ACCEPTANCE_REQUIRED")
        return Probe(backend,True,"READY")
    if backend=="chatterbox":return Probe(backend,False,"POC_UNAVAILABLE")
    raise ValueError("unknown adapter backend")

def fake_synthesize(seconds:float=1.0)->bytes:
    if not 0<seconds<=60:raise ValueError("fake duration out of bounds")
    out=io.BytesIO()
    with wave.open(out,"wb") as wav:
        wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(24000)
        wav.writeframes(b"\0\0"*int(seconds*24000))
    return out.getvalue()

def normalize_wav(payload:bytes)->bytes:
    """Bounded WAV normalization used only by the isolated comparison POC."""
    with wave.open(io.BytesIO(payload),"rb") as src:
        channels,width,rate,frames=src.getnchannels(),src.getsampwidth(),src.getframerate(),src.getnframes()
        if channels not in (1,2) or width not in (1,2,3,4) or rate<8000 or rate>192000 or frames<=0 or frames>rate*60:
            raise ValueError("adapter WAV is unsupported or out of bounds")
        raw=src.readframes(frames)
    if width==1:raw=audioop.bias(raw,1,-128)
    if channels==2:raw=audioop.tomono(raw,width,.5,.5)
    if width!=2:raw=audioop.lin2lin(raw,width,2)
    if rate!=24000:raw,_=audioop.ratecv(raw,2,1,rate,24000,None)
    out=io.BytesIO()
    with wave.open(out,"wb") as dst:
        dst.setnchannels(1);dst.setsampwidth(2);dst.setframerate(24000);dst.writeframes(raw)
    return out.getvalue()

def compare(backend:str, text:str, *, licence_accepted:bool=False)->dict:
    state=probe(backend,licence_accepted=licence_accepted)
    if not state.available:return {"schema":"hq-tts-poc-v1","backend":backend,"status":state.status}
    if not text.strip() or len(text)>4096:raise ValueError("text violates comparison bound")
    start=time.monotonic()
    if backend!="fake":return {"schema":"hq-tts-poc-v1","backend":backend,"status":"POC_UNAVAILABLE"}
    payload=normalize_wav(fake_synthesize())
    return {"schema":"hq-tts-poc-v1","backend":backend,"status":"COMPLETE",
            "wall_s":time.monotonic()-start,"bytes":len(payload),"format":"pcm16-mono-24000",
            "speaker_likeness":"OWNER TEST PENDING","prosody":"OWNER TEST PENDING"}
