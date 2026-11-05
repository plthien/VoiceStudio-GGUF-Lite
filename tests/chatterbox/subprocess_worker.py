"""CI-only framed fake worker executable; never used in production service."""
import base64
import io
import os
import sys
import struct
import time
import wave

from lite.chatterbox_ipc import read_sync, write_sync
from lite.chatterbox_config import MODEL_REVISION

def wav():
    out=io.BytesIO()
    with wave.open(out,"wb") as f:
        f.setnchannels(1);f.setsampwidth(2);f.setframerate(24000)
        f.writeframes(bytes([0,2])*6000)
    return out.getvalue()

def main():
    loads=0; syntheses=0; hits=0; cache=set()
    while True:
        msg=read_sync()
        if msg is None:break
        cmd=msg.get("cmd")
        if cmd=="LOAD":
            loads+=1
            result={"loads":loads,"device":{"name":"SYNTHETIC CI WORKER - NOT CUDA",
                    "index":0,"total_bytes":1,"free_bytes_on_load":1,"evidence":"synthetic_ci"},
                    "model_revision":MODEL_REVISION}
        elif cmd=="SYNTH":
            if msg.get("text")=="__CI_CRASH__":
                os._exit(2)
            if msg.get("text")=="__CI_BAD_FRAME__":
                sys.stdout.buffer.write(struct.pack(">I",0))
                sys.stdout.buffer.flush()
                os._exit(2)
            if msg.get("text")=="__CI_TRUNCATED__":
                sys.stdout.buffer.write(struct.pack(">I",100)+b"xy")
                sys.stdout.buffer.flush()
                os._exit(2)
            if msg.get("text")=="__CI_TIMEOUT__":
                time.sleep(30)
            key=(msg["voice"],msg["version"],msg["reference_sha"])
            if key in cache:hits+=1
            cache.add(key);syntheses+=1
            result={"wav":base64.b64encode(wav()).decode(),
                    "model_id":"chatterbox-nano","backend":"CUDA0","model_revision":MODEL_REVISION,
                    "syntheses":syntheses,"cache_hits":hits,
                    "memory":{}}
        elif cmd=="INVALIDATE":
            voice=msg.get("voice")
            cache={k for k in cache if voice is not None and k[0]!=voice}
            result={"invalidated":True}
        elif cmd=="STATUS":
            result={"loads":loads,"syntheses":syntheses,"cache_hits":hits}
        elif cmd=="EXIT":
            write_sync({"ok":True,"result":{}})
            break
        else:
            write_sync({"ok":False,"error":"WORKER_FAILURE"})
            continue
        write_sync({"ok":True,"result":result})

if __name__=="__main__":
    main()
