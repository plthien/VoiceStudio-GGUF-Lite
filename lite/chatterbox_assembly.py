"""Owner-side sequential long-form requests with atomic output and bounded resume."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
import urllib.parse
import wave
from pathlib import Path

from .audio import wav_info
from .chatterbox_audio import concatenate
from .chatterbox_config import MODEL_REVISION


def atomic(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".chatterbox-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        try: os.unlink(temp)
        except FileNotFoundError: pass


def http_json(base: str, path: str, payload=None, timeout=10):
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as reply:
        return json.load(reply)


def section_key(item, voice, version):
    frozen = {"text": item["input"], "voice": voice, "voice_version": version,
              "model_revision": MODEL_REVISION, "speed": item.get("speed", 1.0),
              "seed": item.get("seed", 42)}
    return hashlib.sha256(json.dumps(frozen, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def do_job(manifest: Path, output: Path, base="http://127.0.0.1:3901",
           resume=False, allow_retry=False, report=None):
    source = json.loads(manifest.read_text(encoding="utf-8"))
    voice = source["voice"]
    sections = source["sections"]
    if not isinstance(sections, list) or not sections:
        raise ValueError("No sections")
    if len(sections) > 200:
        raise ValueError("Too many sections")
    if len({str(x["id"]) for x in sections}) != len(sections):
        raise ValueError("Duplicate section IDs")
    for s in sections:
        if not isinstance(s["input"], str) or not 1 <= len(s["input"]) <= 4096:
            raise ValueError("Section input must be 1..4096 characters")
        if not str(s["id"]).replace("-", "").replace("_", "").isalnum():
            raise ValueError("Unsafe section ID")
    profile = http_json(base, "/profiles/" + urllib.parse.quote(voice, safe=""))
    version = profile["version"]
    status = http_json(base, "/chatterbox/runtime/status")
    if status.get("busy"):
        raise RuntimeError("WORKER_BUSY")
    output.mkdir(parents=True, exist_ok=True)
    state_path = output / "resume-manifest.json"
    if resume and state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("voice") != voice or state.get("voice_version") != version or state.get("model_revision") != MODEL_REVISION:
            raise RuntimeError("RESUME_PROVENANCE_CHANGED")
    elif state_path.exists():
        raise RuntimeError("OUTPUT_EXISTS_USE_RESUME")
    else:
        state = {"schema": "chatterbox-job-v1", "voice": voice, "voice_version": version,
                 "model_revision": MODEL_REVISION, "sections": {}}
    def save():
        atomic(state_path, json.dumps(state, indent=2, sort_keys=True).encode())
    save()
    metrics = []
    for order, s in enumerate(sections):
        identifier = str(s["id"])
        key = section_key(s, voice, version)
        record = state["sections"].get(identifier)
        target = output / ("section-" + identifier + ".wav")
        if resume and record and record.get("state") == "PASS" and record.get("key") == key and target.is_file():
            raw = target.read_bytes()
            if hashlib.sha256(raw).hexdigest() == record.get("sha256"):
                wav_info(raw)
                print(f"SKIP {order+1}/{len(sections)} {identifier} verified", flush=True)
                metrics.append({"id": identifier, "state": "SKIP", "seconds": record["seconds"]})
                continue
            raise RuntimeError("RESUME_WAV_INVALID")
        if record and not allow_retry:
            raise RuntimeError("UNCERTAIN_OR_CHANGED_SECTION_EXPLICIT_RETRY_REQUIRED")
        if record and record.get("key") != key:
            raise RuntimeError("RESUME_INPUT_CHANGED")
        state["sections"][identifier] = {"state": "IN_PROGRESS", "key": key}
        save()
        print(f"START {order+1}/{len(sections)} {identifier}", flush=True)
        started = time.monotonic()
        try:
            body = {"model": "chatterbox-turbo", "voice": voice, "input": s["input"],
                    "response_format": "wav", "language": "en",
                    "speed": s.get("speed", 1.0), "seed": s.get("seed", 42)}
            req = urllib.request.Request(base + "/v1/audio/speech",
                      json.dumps(body).encode(), {"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=1800) as reply:
                data = reply.read(100*1024*1024+1)
                headers = dict(reply.headers)
            info = wav_info(data)
            current = http_json(base, "/profiles/" + urllib.parse.quote(voice, safe=""))
            if current["version"] != version:
                raise RuntimeError("VOICE_VERSION_CHANGED")
            atomic(target, data)
            elapsed = time.monotonic() - started
            entry = {"state": "PASS", "key": key, "sha256": hashlib.sha256(data).hexdigest(),
                     "seconds": info["seconds"], "wall_seconds": elapsed, "rtf": elapsed/info["seconds"],
                     "speed_mode": headers.get("X-TTS-Speed-Mode"),
                     "pause_count": headers.get("X-Lite-Pause-Count"),
                     "backend": headers.get("X-TTS-Backend")}
            state["sections"][identifier] = entry
            save()
            metrics.append({"id":identifier,**entry})
            print(f"PASS {identifier} wall={elapsed:.2f}s audio={info['seconds']:.2f}s rtf={entry['rtf']:.2f}", flush=True)
        except BaseException:
            state["sections"][identifier] = {"state": "UNCERTAIN", "key": key}
            save()
            metrics.append({"id":identifier,"state":"FAIL","wall_seconds":time.monotonic()-started})
            print(f"FAIL {identifier} (no automatic retry)", flush=True)
            if report: atomic(report, json.dumps(metrics,indent=2).encode())
            raise
        finally:
            if report: atomic(report, json.dumps(metrics,indent=2).encode())
    wavs = [ (output / ("section-"+str(s["id"])+".wav")).read_bytes() for s in sections ]
    # Max 100MB for canonical 24k PCM16 final.
    final = concatenate(wavs)
    atomic(output/"final.wav", final)
    info=wav_info(final)
    result = {"state":"PASS","audio_s":info["seconds"],"final_sha256":info["sha256"],"sections":len(sections)}
    atomic(output/"summary.json", json.dumps(result,indent=2).encode())
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--manifest",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--base",default="http://127.0.0.1:3901")
    parser.add_argument("--resume",action="store_true")
    parser.add_argument("--retry-uncertain",action="store_true")
    parser.add_argument("--report",type=Path)
    args=parser.parse_args()
    try:
        print(json.dumps(do_job(args.manifest,args.output,args.base,args.resume,args.retry_uncertain,args.report),
                         indent=2))
    except Exception as exc:
        print("FAIL",type(exc).__name__,str(exc) if str(exc) in (
            "RESUME_PROVENANCE_CHANGED","RESUME_WAV_INVALID","RESUME_INPUT_CHANGED",
            "UNCERTAIN_OR_CHANGED_SECTION_EXPLICIT_RETRY_REQUIRED",
            "VOICE_VERSION_CHANGED","WORKER_BUSY","OUTPUT_EXISTS_USE_RESUME") else "JOB_FAILED")
        raise SystemExit(1)


if __name__=="__main__":
    main()
