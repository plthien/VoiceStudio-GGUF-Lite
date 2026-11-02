"""Owner-only frozen Ink Explainer 306-character benchmark, no generated artefacts in Git."""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import platform
import shutil
import statistics
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .audio import wav_info
from .audio_metrics import measure
from .config import Settings, MODEL_REVISION, RUNTIME_REVISION
from .runtime import sha256, native_files

TEXT=("You lift the lid, toss in a banana peel, and forget about it. But when Paris tried "
"to get people to use covered garbage bins in the eighteen eighties, people got angry. "
"Landlords complained. Newspapers made jokes. And workers who earned their living from "
"discarded stuff worried they might lose their jobs.")
assert len(TEXT)==306
TEXT_SHA256=hashlib.sha256(TEXT.encode("utf-8")).hexdigest()
BASELINE_MEAN_RTF=3.291
LISTENING_FIELDS=("full_coverage_no_omissions_or_repetitions","pronunciation","timbre",
                  "cadence_expression","clipping_or_silence","mastering_trim_watermark_differences")

def _http(url:str,body:dict|None=None):
    data=json.dumps(body,ensure_ascii=False).encode("utf-8") if body is not None else None
    request=urllib.request.Request(url,data=data,
        headers={"Content-Type":"application/json"} if body is not None else {},
        method="POST" if body is not None else "GET")
    with urllib.request.urlopen(request,timeout=1200) as reply:
        return reply.read(),dict(reply.headers.items())

def _json(url):
    payload,_=_http(url)
    return json.loads(payload)

def _command_version(executable,*args):
    path=shutil.which(executable)
    if not path:
        return None
    try:
        proc=subprocess.run([path,*args],capture_output=True,text=True,errors="replace",timeout=10)
        return proc.stdout.splitlines()[0][:180] if proc.returncode==0 and proc.stdout else None
    except (OSError,subprocess.TimeoutExpired):
        return None

def _device_proof(headers):
    normalized={k.lower():v for k,v in headers.items()}
    backend=normalized.get("x-lite-verified-backend")
    device=normalized.get("x-lite-observed-device","")
    verified=normalized.get("x-lite-verification-status")
    if backend!="Vulkan0" or verified!="verified" or "NVIDIA" not in device or "GTX 1650" not in device:
        raise ValueError("Generated response lacks per-inference NVIDIA Vulkan0 proof")
    return {"observed_native_backend":backend,"observed_device":device,"verification_status":verified}

def run(voice_id:str,output:Path,port:int=3900,repetitions:int=3,policy:str="warm"):
    if repetitions!=3 or not 1<=port<=65535 or not voice_id.strip() or policy not in ("warm","cold"):
        raise ValueError("Frozen benchmark requires voice ID, port, warm/cold policy and 3 runs")
    base="http://127.0.0.1:"+str(port)
    ready=_json(base+"/lite/readiness")
    quant=ready.get("quant",{})
    if not (ready.get("ready") and quant.get("verified") and quant.get("prepared_quant")=="Q8_0"
            and quant.get("in_use") is None and ready.get("gpu",{}).get("verified")):
        raise ValueError("Pinned Q8_0 model, GPU or licence is not ready")
    profile=_json(base+"/profiles/"+urllib.parse.quote(voice_id,safe=""))
    if profile.get("id")!=voice_id or not profile.get("ref_sha256") or not profile.get("source_sha256"):
        raise ValueError("Stable voice reference identity is missing")
    settings=Settings.load()
    exe,dll=native_files(settings)
    pins=json.loads((Path(__file__).resolve().parent/"resources/q8_pins.json").read_text(encoding="utf-8"))
    output.mkdir(parents=True,exist_ok=True)
    report={"benchmark_case_id":"Ink-Explainer-306","source_head_sha":None,
       "narration_sha256":TEXT_SHA256,"character_count":306,"seed":42,
       "profile_id":voice_id,"profile_version":profile["version"],
       "ref_audio_sha256":profile["source_sha256"],"normalized_ref_sha256":profile["ref_sha256"],
       "ref_text_sha256":hashlib.sha256(profile["ref_text"].encode("utf-8")).hexdigest(),
       "language":profile["language"],"model_revision":MODEL_REVISION,
       "runtime_revision":RUNTIME_REVISION,
       "base_sha256":pins["base"]["sha256"],"tokenizer_sha256":pins["tokenizer"]["sha256"],
       "exe_sha256":sha256(exe),"dll_sha256":sha256(dll),
       "requested_backend":"Vulkan0","gpu":ready["gpu"],
       "os":platform.platform(),"python":platform.python_version(),
       "ffmpeg_version":_command_version("ffmpeg","-version"),
       "nvidia_driver":_command_version("nvidia-smi","--query-gpu=driver_version","--format=csv,noheader"),
       "vulkaninfo_version":_command_version("vulkaninfo","--version"),
       "cold_warm_policy":policy,"runs":[],"comparability":False,
       "comparison_reason":"Original VoiceStudio reference/transcript, flags and DSP chain not independently matched",
       "historical_full_voicestudio_mean_rtf":BASELINE_MEAN_RTF,
       "human_listening":{"status":"NOT VERIFIED","criteria":list(LISTENING_FIELDS)}}
    try:
        for n in range(1,4):
            body={"model":"omnivoice-gguf","voice":voice_id,"input":TEXT,
                  "response_format":"wav","speed":1.0,"language":profile["language"],"seed":42}
            start=time.monotonic()
            payload,headers=_http(base+"/v1/audio/speech",body)
            elapsed=time.monotonic()-start
            proof=_device_proof(headers)
            info=wav_info(payload)
            dest=output/("run-%02d.wav"%n)
            dest.write_bytes(payload)
            metrics=measure(dest)
            report["runs"].append({"run":n,"elapsed_s":elapsed,"audio_s":info["seconds"],
                "rtf":elapsed/info["seconds"],"wav_sha256":info["sha256"],
                "size_bytes":info["bytes"],"metrics":metrics,**proof,
                "recorded_at":dt.datetime.now(dt.timezone.utc).isoformat()})
        report["mean_rtf"]=statistics.mean(x["rtf"] for x in report["runs"])
        report["median_rtf"]=statistics.median(x["rtf"] for x in report["runs"])
        report["spread_rtf"]=max(x["rtf"] for x in report["runs"])-min(x["rtf"] for x in report["runs"])
        report["status"]="3_OF_3_NATIVE_VERIFIED"
    except Exception as err:
        report["status"]="FAILED"
        report["error_code"]=type(err).__name__
        (output/"benchmark.json").write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding="utf-8")
        raise
    (output/"benchmark.json").write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding="utf-8")
    return report

def main():
    parser=argparse.ArgumentParser(description="User-host-only Q8_0 Ink Explainer benchmark")
    parser.add_argument("--voice-id",required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--port",type=int,default=3900)
    parser.add_argument("--repetitions",type=int,default=3)
    parser.add_argument("--policy",choices=("warm","cold"),default="warm")
    args=parser.parse_args()
    try:
        report=run(args.voice_id,args.output,args.port,args.repetitions,args.policy)
        print(json.dumps({"status":report["status"],"mean_rtf":report["mean_rtf"],
                          "median_rtf":report["median_rtf"],"output":str(args.output)}))
        return 0
    except Exception as err:
        print("Benchmark FAILED: "+type(err).__name__+"; private diagnostics in output/benchmark.json",file=sys.stderr)
        return 2

if __name__=="__main__":
    raise SystemExit(main())
