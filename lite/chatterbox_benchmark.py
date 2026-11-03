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
from .chatterbox_assembly import atomic, do_job, load_source


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
                      model_revision=headers.get("X-TTS-Model-Revision"),voice_version=headers.get("X-TTS-Voice-Version"),
                      cuda_allocated_bytes=headers.get("X-TTS-CUDA-Allocated-Bytes"),
                      cuda_peak_bytes=headers.get("X-TTS-CUDA-Peak-Bytes"),
                      cuda_free_bytes=headers.get("X-TTS-CUDA-Free-Bytes"))
        if output:
            atomic(output/(name+".wav"),data)
    except Exception as exc:
        record["error"]=type(exc).__name__
    record["wall_s"]=time.monotonic()-started
    print(f"{record['state']} {name} wall={record['wall_s']:.2f}s",flush=True)
    return record


def evaluate_h07(audio_s, wall_s, cold_start_included=True):
    """H07 is a performance qualification, not merely completed synthesis."""
    rtf=wall_s/audio_s if audio_s>0 else None
    if not cold_start_included:
        status,reason="NOT_VERIFIED","COLD_START_NOT_INCLUDED"
    elif audio_s<600:
        status,reason="NOT_VERIFIED","AUDIO_UNDER_600_SECONDS"
    elif wall_s>1800 or rtf is None or rtf>3.0:
        status,reason="FAIL","H07_SLA_EXCEEDED"
    else:
        status,reason="PASS","H07_MEASURED_THRESHOLDS_MET"
    return {"h07_status":status,"h07_reason":reason,"audio_s":audio_s,
            "wall_s":wall_s,"end_to_end_rtf":rtf,
            "cold_start_included":cold_start_included}


def save_reports(folder,records):
    """Always refresh both reports, including early error or SLA miss."""
    atomic(folder/"owner-metrics.json",json.dumps(records,indent=2).encode())
    fields=["case","state","generation_state","h07_status","h07_reason",
            "cold_start_included","wall_s","audio_s","end_to_end_rtf","rtf",
            "voice_version","model_revision","speech_s","pause_s","backend",
            "cuda_allocated_bytes","cuda_peak_bytes","cuda_free_bytes","error"]
    out=io.StringIO()
    writer=csv.DictWriter(out,fieldnames=fields,extrasaction="ignore")
    writer.writeheader();writer.writerows(records)
    atomic(folder/"owner-metrics.csv",out.getvalue().encode("utf-8"))


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
    records=[]
    try:
        # F02: no runtime request (including an All-mode smoke) before voice match.
        if a.mode in ("LongForm","All"):
            if a.manifest is None:
                raise ValueError("MANIFEST_REQUIRED")
            load_source(a.manifest,expected_voice=a.voice_id)
        def force_cold_start():
            req=urllib.request.Request(a.base+"/chatterbox/runtime/unload",data=b"{}",
                    headers={"Content-Type":"application/json"},method="POST")
            with urllib.request.urlopen(req,timeout=15) as resp:
                if resp.status!=200:raise RuntimeError("COLD_UNLOAD_FAILED")

        short="Something was moving beyond the doorway. The group stopped and listened carefully."
        cases=[]
        if a.mode in ("Smoke","All"):cases.append(("smoke",short))
        if a.mode in ("ColdWarm","All"):cases.extend((("cold",short),("warm_1",short),("warm_2",short)))
        if a.mode in ("Pause","All"):cases.append(("pause","He opened the door. [pause 250ms] It moved. [pause 1.5s] Run."))
        failed=False
        for name,text in cases:
            print("START",name,flush=True)
            if name=="cold":
                try:force_cold_start()
                except Exception:
                    records.append({"case":name,"state":"FAIL","generation_state":"FAIL",
                                    "error":"COLD_UNLOAD_FAILED"})
                    failed=True;break
            row=run_case(name,text,a.voice_id,a.base,a.output)
            row["generation_state"]=row["state"]
            records.append(row)
            save_reports(folder,records)
            if row["state"]!="PASS":
                failed=True;break
        if not failed and a.mode in ("LongForm","All"):
            print("START longform cold-start",flush=True)
            try:force_cold_start()
            except Exception:
                records.append({"case":"longform","state":"FAIL","generation_state":"FAIL",
                                "h07_status":"NOT_VERIFIED","h07_reason":"COLD_UNLOAD_FAILED",
                                "cold_start_included":False,"error":"COLD_UNLOAD_FAILED"})
            else:
                started=time.monotonic()
                try:
                    result=do_job(a.manifest,folder/"longform",a.base,resume=False,
                                  report=folder/"sections.json",expected_voice=a.voice_id)
                    elapsed=time.monotonic()-started
                    assessment=evaluate_h07(result["audio_s"],elapsed,True)
                    records.append({"case":"longform","state":"PASS","generation_state":"PASS",
                                    "rtf":assessment["end_to_end_rtf"],**result,**assessment})
                except Exception as exc:
                    records.append({"case":"longform","state":"FAIL","generation_state":"FAIL",
                                    "h07_status":"NOT_VERIFIED","h07_reason":"GENERATION_FAILED",
                                    "cold_start_included":True,"wall_s":time.monotonic()-started,
                                    "error":type(exc).__name__})
    except Exception as exc:
        code=str(exc)
        if code not in {"MANIFEST_REQUIRED","BENCHMARK_VOICE_MISMATCH",
                         "BENCHMARK_VOICE_INVALID","INVALID_SECTIONS","INVALID_SECTION",
                         "INVALID_SECTION_ID","DUPLICATE_SECTION_ID","INVALID_SECTION_INPUT"}:
            code="BENCHMARK_INPUT_INVALID"
        records.append({"case":"longform" if a.mode in ("LongForm","All") else "benchmark",
                        "state":"FAIL","generation_state":"FAIL","h07_status":"NOT_VERIFIED",
                        "h07_reason":code,"error":code})
    finally:
        save_reports(folder,records)
        print("RESULTS",str(folder/"owner-metrics.json"),flush=True)
    return 0 if records and all(r.get("state")=="PASS" and
            (r.get("h07_status")=="PASS" if r.get("case")=="longform" else True)
            for r in records) else 1


if __name__=="__main__":
    raise SystemExit(main())
