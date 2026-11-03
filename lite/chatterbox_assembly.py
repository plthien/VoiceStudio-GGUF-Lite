"""Owner-side sequential long-form requests with atomic output and bounded resume."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import contextlib
import os
import tempfile
import time
import urllib.error
import urllib.request
import urllib.parse
import wave
from pathlib import Path

from .audio import wav_info
from .chatterbox_audio import concatenate, FINAL_MAX_SECONDS, FINAL_MAX_BYTES
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


def section_key(item, voice, version, reference_binding=None):
    frozen = {"text": item["input"], "voice": voice, "voice_version": version,
              "reference_binding": reference_binding,
              "model_revision": MODEL_REVISION, "speed": item.get("speed", 1.0),
              "seed": item.get("seed", 42)}
    return hashlib.sha256(json.dumps(frozen, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def reference_binding(profile: dict, voice: str) -> str:
    """Private opaque identity spanning DB provenance; never log source checksums."""
    if not isinstance(profile, dict) or profile.get("id") != voice:
        raise RuntimeError("REFERENCE_PROVENANCE_UNAVAILABLE")
    version = profile.get("version")
    ref_sha = profile.get("ref_sha256")
    source_sha = profile.get("source_sha256")
    if (type(version) is not int or version < 1 or
        not isinstance(ref_sha, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", ref_sha) or
        not isinstance(source_sha, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", source_sha)):
        raise RuntimeError("REFERENCE_PROVENANCE_UNAVAILABLE")
    frozen = {"voice": voice, "version": version, "ref_sha256": ref_sha.lower(),
              "source_sha256": source_sha.lower(), "model_revision": MODEL_REVISION}
    return hashlib.sha256(json.dumps(frozen, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _do_job_unlocked(source: dict, output: Path, base="http://127.0.0.1:3901",
           resume=False, allow_retry=False, report=None):
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
    binding = reference_binding(profile, voice)
    def assert_reference_unchanged():
        latest = http_json(base, "/profiles/" + urllib.parse.quote(voice, safe=""))
        if reference_binding(latest, voice) != binding:
            raise RuntimeError("RESUME_PROVENANCE_CHANGED")
    status = http_json(base, "/chatterbox/runtime/status")
    if status.get("busy"):
        raise RuntimeError("WORKER_BUSY")
    output.mkdir(parents=True, exist_ok=True)
    state_path = output / "resume-manifest.json"
    job_key=hashlib.sha256(json.dumps({
        "voice":voice,"voice_version":version,"model_revision":MODEL_REVISION,
        "reference_binding": binding,
        "ordered_sections":[(v["id"],section_key(v,voice,version,binding)) for v in sections]
    },sort_keys=True,separators=(",",":")).encode()).hexdigest()
    if state_path.is_symlink(): raise RuntimeError("OUTPUT_PATH_UNTRUSTED")
    if resume and state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("voice") != voice or state.get("voice_version") != version or state.get("model_revision") != MODEL_REVISION or state.get("job_key") != job_key or state.get("reference_binding") != binding:
            raise RuntimeError("RESUME_PROVENANCE_CHANGED")
    elif state_path.exists():
        raise RuntimeError("OUTPUT_EXISTS_USE_RESUME")
    else:
        state = {"schema": "chatterbox-job-v1", "voice": voice, "voice_version": version,
                 "model_revision": MODEL_REVISION, "job_key":job_key,
                 "reference_binding":binding,"sections": {}}
    def save():
        atomic(state_path, json.dumps(state, indent=2, sort_keys=True).encode())
    save()
    metrics = []
    for order, s in enumerate(sections):
        identifier = str(s["id"])
        key = section_key(s, voice, version, binding)
        assert_reference_unchanged()
        record = state["sections"].get(identifier)
        target = output / ("section-" + identifier + ".wav")
        if target.is_symlink(): raise RuntimeError("OUTPUT_PATH_UNTRUSTED")
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
                headers = reply.headers  # Case-insensitive HTTPMessage; dict conversion loses header lookups.
            info = wav_info(data)
            assert_reference_unchanged()
            atomic(target, data)
            elapsed = time.monotonic() - started
            entry = {"state": "PASS", "key": key, "sha256": hashlib.sha256(data).hexdigest(),
                     "seconds": info["seconds"], "wall_seconds": elapsed, "rtf": elapsed/info["seconds"],
                     "speed_mode": headers.get("X-TTS-Speed-Mode"),
                     "pause_count": headers.get("X-Lite-Pause-Count"),
                     "backend": headers.get("X-TTS-Backend"),
                     "cuda_allocated_bytes": headers.get("X-TTS-CUDA-Allocated-Bytes"),
                     "cuda_peak_bytes": headers.get("X-TTS-CUDA-Peak-Bytes"),
                     "cuda_free_bytes": headers.get("X-TTS-CUDA-Free-Bytes"),
                     "speed": s.get("speed", 1.0), "voice_version": version}
            state["sections"][identifier] = entry
            save()
            metrics.append({"id":identifier,**entry})
            mem = entry["cuda_peak_bytes"] or "NOT_MEASURED"
            print(f"PASS {identifier} wall={elapsed:.2f}s audio={info['seconds']:.2f}s rtf={entry['rtf']:.2f} "
                  f"speed={entry['speed']} pause={entry['pause_count']} peak_vram_bytes={mem}",
                  flush=True)
        except BaseException:
            state["sections"][identifier] = {"state": "UNCERTAIN", "key": key}
            save()
            metrics.append({"id":identifier,"state":"FAIL","wall_seconds":time.monotonic()-started})
            print(f"FAIL {identifier} (no automatic retry)", flush=True)
            if report: atomic(report, json.dumps(metrics,indent=2).encode())
            raise
        finally:
            if report: atomic(report, json.dumps(metrics,indent=2).encode())
    assert_reference_unchanged()
    wavs = [ (output / ("section-"+str(s["id"])+".wav")).read_bytes() for s in sections ]
    # Final assembly is independently bounded to 15 minutes; no unverified WAV is published.
    final = concatenate(wavs)
    info=wav_info(final, max_bytes=FINAL_MAX_BYTES, max_seconds=FINAL_MAX_SECONDS)
    assert_reference_unchanged()
    result = {"state":"PASS","audio_s":info["seconds"],"final_sha256":info["sha256"],"sections":len(sections),
              "voice_version":version,"model_revision":MODEL_REVISION}
    target_final=output/"final.wav"
    target_summary=output/"summary.json"
    if target_final.is_symlink() or target_summary.is_symlink():
        raise RuntimeError("OUTPUT_PATH_UNTRUSTED")
    try:
        atomic(target_final, final)
        atomic(target_summary, json.dumps(result, indent=2).encode())
    except BaseException:
        # Incomplete publish is never advertised as a successful final result.
        target_final.unlink(missing_ok=True)
        target_summary.unlink(missing_ok=True)
        raise
    return result



def load_source(manifest: Path, expected_voice: str | None = None) -> dict:
    """Validate benchmark identity and section naming before network or output effects."""
    source=json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(source,dict):
        raise ValueError("BENCHMARK_VOICE_INVALID")
    voice=source.get("voice")
    if not isinstance(voice,str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}",voice):
        raise ValueError("BENCHMARK_VOICE_INVALID")
    if expected_voice is not None and (not isinstance(expected_voice,str) or voice!=expected_voice):
        raise ValueError("BENCHMARK_VOICE_MISMATCH")
    sections=source.get("sections")
    if not isinstance(sections,list) or not 1<=len(sections)<=200:
        raise ValueError("INVALID_SECTIONS")
    seen=set()
    for part in sections:
        if not isinstance(part,dict):
            raise ValueError("INVALID_SECTION")
        identifier=part.get("id")
        if not isinstance(identifier,str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}",identifier):
            raise ValueError("INVALID_SECTION_ID")
        if identifier in seen:
            raise ValueError("DUPLICATE_SECTION_ID")
        seen.add(identifier)
        data=part.get("input")
        if not isinstance(data,str) or not data.strip() or len(data)>4096:
            raise ValueError("INVALID_SECTION_INPUT")
    return source


@contextlib.contextmanager
def output_job_lock(output: Path):
    """Exclusive OS-lock across processes; crash releases FD without stale-file deletion."""
    output.mkdir(parents=True,exist_ok=True)
    if output.is_symlink(): raise RuntimeError("OUTPUT_PATH_UNTRUSTED")
    lock_path=output/".chatterbox-job.lock"
    if lock_path.is_symlink(): raise RuntimeError("OUTPUT_PATH_UNTRUSTED")
    with lock_path.open("a+b") as fd:
        if fd.seek(0,2)==0:
            fd.write(bytes([0]));fd.flush()
        fd.seek(0)
        acquired=False
        try:
            try:
                if os.name=="nt":
                    import msvcrt
                    msvcrt.locking(fd.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(fd.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                acquired=True
            except OSError as exc:
                raise RuntimeError("OUTPUT_JOB_BUSY") from exc
            yield
        finally:
            if acquired:
                fd.seek(0)
                if os.name=="nt":
                    msvcrt.locking(fd.fileno(),msvcrt.LK_UNLCK,1)
                else:
                    fcntl.flock(fd.fileno(),fcntl.LOCK_UN)


def do_job(manifest: Path, output: Path, base="http://127.0.0.1:3901",
           resume=False, allow_retry=False, report=None, expected_voice=None):
    source=load_source(manifest,expected_voice=expected_voice)
    with output_job_lock(output):
        return _do_job_unlocked(source,output,base,resume,allow_retry,report)


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
            "VOICE_VERSION_CHANGED","WORKER_BUSY","OUTPUT_EXISTS_USE_RESUME",
             "OUTPUT_JOB_BUSY","BENCHMARK_VOICE_MISMATCH","BENCHMARK_VOICE_INVALID",
             "REFERENCE_PROVENANCE_UNAVAILABLE") else "JOB_FAILED")
        raise SystemExit(1)


if __name__=="__main__":
    main()
