"""Owner-only reports, one visible case at a time. No CI acoustic inference claims."""
from __future__ import annotations
import argparse
import csv
import io
import json
import os
import tempfile
import time
import urllib.request
from pathlib import Path

from .audio import wav_info
from .chatterbox_assembly import atomic, do_job


def run_case(name, text, voice, base, output):
    started = time.monotonic()
    record = {"case": name, "state": "FAIL", "wall_s": 0.0}
    try:
        payload = {"model":"chatterbox-turbo","voice":voice,"input":text,
                   "response_format":"wav","language":"en","speed":1.0,"seed":42}
        req=urllib.request.Request(base+"/v1/audio/speech",json.dumps(payload).encode(),
                                   {"Content-Type":"application/json"},method="POST")
        with urllib.request.urlopen(req,timeout=1800) as reply:
            data=reply.read(100*1024*1024+1)
            headers=reply.headers
        info=wav_info(data)
        record.update(state="PASS",audio_s=info["seconds"],speech_s=float(headers.get("X-Lite-Speech-S",0)),
                      pause_s=float(headers.get("X-Lite-Pause-S",0)),
                      rtf=(time.monotonic()-started)/info["seconds"],backend=headers.get("X-TTS-Backend"),
                      model_revision=headers.get("X-TTS-Model-Revision"),voice_version=headers.get("X-TTS-Voice-Version"))
        if output:
            atomic(output/(name+".wav"),data)
    except Exception as exc:
        record["error"]=type(exc).__name__
    record["wall_s"]=time.monotonic()-started
    print(f"{record['state']} {name} wall={record['wall_s']:.2f}s",flush=True)
    return record


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--voice-id",required=True)
    p.add_argument("--mode",choices=["Smoke","ColdWarm","Pause","LongForm","All"],default="Smoke")
    p.add_argument("--manifest",type=Path)
    p.add_argument("--output",type=Path)
    p.add_argument("--base",default="http://127.0.0.1:3901")
    a=p.parse_args()
    folder=a.output or Path(os.environ.get("CHATTERBOX_ROOT",r"D:\VoiceStudioBuild\chatterbox-turbo"))/"benchmarks"
    folder.mkdir(parents=True,exist_ok=True)
    short="Something was moving beyond the doorway. The group stopped and listened carefully."
    cases = []
    if a.mode in ("Smoke","All"):
        cases.append(("smoke", short))
    if a.mode in ("ColdWarm","All"):
        cases.extend((("cold",short),("warm_1",short),("warm_2",short)))
    if a.mode in ("Pause","All"):
        cases.append(("pause", "He opened the door. [pause 250ms] It moved. [pause 1.5s] Run."))
    records=[]
    for name,text in cases:
        print(f"START {name}",flush=True)
        if name=="cold":
            try:
                req=urllib.request.Request(a.base+"/chatterbox/runtime/unload",data=b"{}",
                                           headers={"Content-Type":"application/json"},method="POST")
                with urllib.request.urlopen(req,timeout=15) as response:
                    if response.status!=200:
                        raise RuntimeError("UNLOAD_FAILED")
            except Exception:
                records.append({"case":name,"state":"FAIL","error":"COLD_UNLOAD_FAILED"})
                atomic(folder/"owner-metrics.json",json.dumps(records,indent=2).encode())
                break
        records.append(run_case(name,text,a.voice_id,a.base,a.output))
        atomic(folder/"owner-metrics.json",json.dumps(records,indent=2).encode())
    if a.mode in ("LongForm","All"):
        if a.manifest is None:
            records.append({"case":"longform","state":"FAIL","error":"MANIFEST_REQUIRED"})
        else:
            started=time.monotonic()
            try:
                result=do_job(a.manifest,folder/"longform",a.base,resume=False,report=folder/"sections.json")
                elapsed=time.monotonic()-started
                records.append({"case":"longform","state":"PASS","wall_s":elapsed,
                                "rtf":elapsed/result["audio_s"], "sla_checked":result["audio_s"]>=600,
                                "sla_under_1800s": elapsed<=1800 if result["audio_s"]>=600 else None,
                                **result})
            except Exception as exc:
                records.append({"case":"longform","state":"FAIL","wall_s":time.monotonic()-started,
                                "error":type(exc).__name__})
    atomic(folder/"owner-metrics.json",json.dumps(records,indent=2).encode())
    # Always write CSV including failures and empty results.
    fields=["case","state","wall_s","audio_s","speech_s","pause_s","rtf","backend","error"]
    with tempfile.NamedTemporaryFile(mode="w",newline="",encoding="utf-8",dir=folder,delete=False) as f:
        writer=csv.DictWriter(f,fieldnames=fields,extrasaction="ignore")
        writer.writeheader()
        for r in records:writer.writerow(r)
        tmp=f.name
    os.replace(tmp,folder/"owner-metrics.csv")
    print("RESULTS",str(folder/"owner-metrics.json"),flush=True)
    return 0 if records and all(r["state"]=="PASS" for r in records) else 1


if __name__=="__main__":
    raise SystemExit(main())
