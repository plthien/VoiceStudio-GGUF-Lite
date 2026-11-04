"""Owner-side sequential long-form requests with atomic output and bounded resume."""
from __future__ import annotations

import argparse
import hashlib
import hmac
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
import uuid
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


def http_json(base: str, path: str, payload=None, timeout=10, method=None):
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, body, headers={"Content-Type": "application/json"}, method=method)
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



# A single atomic receipt is the publication authority. The owner-visible
# final.wav/summary.json are projections, rebuilt from immutable verified
# version blobs after a crash between their two replacements.
FINAL_RECEIPT = "final-commit.json"
FINAL_VERSIONS = ".chatterbox-final-versions"
FINAL_SCHEMA = "chatterbox-final-commit-v1"


def _final_version(output: Path, artifact_id: str) -> Path:
    if not isinstance(artifact_id, str) or not re.fullmatch(r"[a-f0-9]{32}",artifact_id):
        raise RuntimeError("FINAL_PUBLICATION_RECOVERY_REQUIRED")
    root=output/FINAL_VERSIONS
    folder=root/artifact_id
    if root.is_symlink() or folder.is_symlink():
        raise RuntimeError("FINAL_PUBLICATION_RECOVERY_REQUIRED")
    return folder


def _verify_final_bytes(wav: bytes, summary: bytes) -> dict:
    try:
        result=json.loads(summary)
        info=wav_info(wav,max_bytes=FINAL_MAX_BYTES,max_seconds=FINAL_MAX_SECONDS)
        if (not isinstance(result,dict) or result.get("state")!="PASS"
            or result.get("final_sha256")!=info["sha256"]
            or result.get("audio_s")!=info["seconds"]
            or type(result.get("sections")) is not int
            or result["sections"]<1):
            raise ValueError("invalid final-summary agreement")
        return result
    except (ValueError,TypeError,KeyError,OSError):
        raise RuntimeError("FINAL_PUBLICATION_RECOVERY_REQUIRED") from None


def _read_commit(output: Path, job_key: str | None = None):
    receipt=output/FINAL_RECEIPT
    if receipt.is_symlink():
        raise RuntimeError("FINAL_PUBLICATION_RECOVERY_REQUIRED")
    if not receipt.exists():
        return None
    try:
        data=json.loads(receipt.read_bytes())
        if (not isinstance(data,dict) or data.get("schema")!=FINAL_SCHEMA
            or not isinstance(data.get("job_key"),str)
            or (job_key is not None and data["job_key"]!=job_key)):
            raise ValueError("receipt identity mismatch")
        folder=_final_version(output,data["artifact_id"])
        wave_path=folder/"final.wav"
        summary_path=folder/"summary.json"
        if wave_path.is_symlink() or summary_path.is_symlink():
            raise ValueError("untrusted version")
        wav=wave_path.read_bytes()
        summary=summary_path.read_bytes()
        if (hashlib.sha256(wav).hexdigest()!=data.get("wav_sha256")
            or hashlib.sha256(summary).hexdigest()!=data.get("summary_sha256")):
            raise ValueError("receipt checksum mismatch")
        result=_verify_final_bytes(wav,summary)
        return data,wav,summary,result
    except (OSError,ValueError,KeyError,TypeError,RuntimeError):
        raise RuntimeError("FINAL_PUBLICATION_RECOVERY_REQUIRED") from None


def _public_paths(output: Path):
    wav=output/"final.wav"
    summary=output/"summary.json"
    if wav.is_symlink() or summary.is_symlink():
        raise RuntimeError("OUTPUT_PATH_UNTRUSTED")
    return wav,summary


def _restore_public(output: Path, old):
    """Reconcile a partially overwritten public pair from the old immutable commit."""
    target_wav,target_summary=_public_paths(output)
    try:
        if old is None:
            # A first-ever failed publish has no committed pair.
            target_wav.unlink(missing_ok=True)
            target_summary.unlink(missing_ok=True)
        else:
            _,wav,summary,_=old
            if not target_wav.is_file() or target_wav.read_bytes()!=wav:
                atomic(target_wav,wav)
            if not target_summary.is_file() or target_summary.read_bytes()!=summary:
                atomic(target_summary,summary)
    except BaseException as exc:
        # Keep version blobs and old receipt for an explicit recovery on restart.
        raise RuntimeError("FINAL_PUBLICATION_RECOVERY_REQUIRED") from exc


def _ensure_public_commit(output: Path, receipt):
    if receipt is not None:
        _restore_public(output,receipt)


def _trusted_legacy_pair(output: Path, job_key: str, state: dict, seconds: float):
    """Read pre-V3 job outputs without forcing an owner migration."""
    wav_path,summary_path=_public_paths(output)
    if not wav_path.exists() and not summary_path.exists():
        return None
    if not wav_path.is_file() or not summary_path.is_file():
        return None
    try:
        wav=wav_path.read_bytes()
        summary=summary_path.read_bytes()
        result=_verify_final_bytes(wav,summary)
        if (result.get("model_revision")!=MODEL_REVISION
            or result.get("voice_version")!=state["voice_version"]
            or result.get("sections")!=len(state["sections"])
            or result.get("audio_s")!=seconds):
            return None
        return result
    except (OSError,RuntimeError,KeyError):
        return None


def _existing_final_result(output: Path, state: dict, job_key: str,
                           total_frames: int, section_count: int, ordered_ids: list[str]):
    old=_read_commit(output,job_key)
    if old is not None:
        receipt,wav,summary,result=old
        if (result.get("model_revision") != MODEL_REVISION
            or result.get("voice_version") != state["voice_version"]
            or result["sections"] != section_count
            or result["audio_s"] != total_frames/24000):
            raise RuntimeError("FINAL_PUBLICATION_RECOVERY_REQUIRED")
        _ensure_public_commit(output,old)
        return result
    # Old V1 jobs have no authoritative receipt. Only their independently
    # verified *complete pair* can be accepted, with matching job/section state.
    if state.get("schema")=="chatterbox-job-v1":
        legacy=_trusted_legacy_pair(output,job_key,state,total_frames/24000)
        if legacy is None:
            return None
        # Older V2 jobs have no receipt: only accept when bytes also agree with
        # the independently checksum-verified ordered section PCM, not merely
        # a potentially edited final WAV + edited summary.
        def ordered_legacy():
            for identifier in ordered_ids:
                target=output/("section-"+identifier+".wav")
                if target.is_symlink():
                    raise RuntimeError("OUTPUT_PATH_UNTRUSTED")
                yield target.read_bytes()
        rebuilt=concatenate(ordered_legacy())
        if hashlib.sha256(rebuilt).hexdigest()!=legacy["final_sha256"]:
            return None
        return legacy
    return None


def publish_final_pair(output: Path, final: bytes, result: dict, *, job_key: str):
    """Stage verified immutable blobs, then project views, receipt LAST.

    A single receipt decides whether a new version committed. A crash after the
    first projection leaves an older receipt whose immutable blobs permit repair.
    Failed rollback raises a typed recovery-required state; originals survive
    as immutable blobs rather than being deleted.
    """
    _verify_final_bytes(final,json.dumps(result,indent=2).encode())
    old=_read_commit(output,job_key)
    if old is not None:
        _ensure_public_commit(output,old)
    target_wav,target_summary=_public_paths(output)
    receipt_path=output/FINAL_RECEIPT
    versions=output/FINAL_VERSIONS
    if versions.is_symlink() or receipt_path.is_symlink():
        raise RuntimeError("OUTPUT_PATH_UNTRUSTED")
    versions.mkdir(exist_ok=True)
    # A receiptless pair is NEVER promoted into a trusted commit merely
    # because its WAV SHA matches an editable summary. Legacy no-write reuse
    # is separately proven against ordered verified section audio above.
    new_id=uuid.uuid4().hex
    new_dir=_final_version(output,new_id)
    new_dir.mkdir(exist_ok=False)
    summary_bytes=json.dumps(result,indent=2).encode()
    # All stage I/O and fsync must succeed before touching owner-visible files.
    atomic(new_dir/"final.wav",final)
    atomic(new_dir/"summary.json",summary_bytes)
    candidate={"schema":FINAL_SCHEMA,"artifact_id":new_id,"job_key":job_key,
               "wav_sha256":hashlib.sha256(final).hexdigest(),
               "summary_sha256":hashlib.sha256(summary_bytes).hexdigest()}
    receipt_bytes=json.dumps(candidate,sort_keys=True).encode()
    try:
        atomic(target_wav,final)
        atomic(target_summary,summary_bytes)
        # Verify the projection before committing its authoritative receipt.
        if target_wav.read_bytes()!=final or target_summary.read_bytes()!=summary_bytes:
            raise RuntimeError("FINAL_PUBLICATION_RECOVERY_REQUIRED")
        atomic(receipt_path,receipt_bytes)
    except BaseException as exc:
        try:
            # If replacement of receipt succeeded but cleanup failed, do not
            # roll back its now-committed version. Report recovery-required.
            if receipt_path.is_file() and receipt_path.read_bytes()==receipt_bytes:
                raise RuntimeError("FINAL_PUBLICATION_RECOVERY_REQUIRED") from exc
            _restore_public(output,old)
        except BaseException as rollback:
            raise RuntimeError("FINAL_PUBLICATION_RECOVERY_REQUIRED") from rollback
        raise
    return result


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
    try:
        profile = http_json(base, "/profiles/" + urllib.parse.quote(voice, safe=""))
        version = profile["version"]
    except (OSError, urllib.error.URLError, TypeError, ValueError, KeyError):
        raise RuntimeError("REFERENCE_CHANGED") from None
    binding = reference_binding(profile, voice)
    def assert_reference_unchanged():
        endpoint = "/profiles/" + urllib.parse.quote(voice, safe="")
        try:
            latest = http_json(base, endpoint)
            if reference_binding(latest,voice) != binding:
                raise RuntimeError("RESUME_PROVENANCE_CHANGED")
            proof = http_json(base, endpoint + "/integrity/verify", method="POST")
            if (not isinstance(proof,dict) or proof.get("verified") is not True
                or not isinstance(proof.get("binding"),str)
                or not hmac.compare_digest(proof["binding"],binding)):
                raise RuntimeError("REFERENCE_CHANGED")
        except RuntimeError:
            raise
        except (OSError, urllib.error.URLError, ValueError, KeyError, TypeError):
            raise RuntimeError("REFERENCE_CHANGED") from None
    assert_reference_unchanged()
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
        state = {"schema": "chatterbox-job-v2", "voice": voice, "voice_version": version,
                 "model_revision": MODEL_REVISION, "job_key":job_key,
                 "reference_binding":binding,"sections": {}}
    def save():
        atomic(state_path, json.dumps(state, indent=2, sort_keys=True).encode())
    if not (resume and state_path.exists()):
        save()
    # Validate a previous commit before accepting ANY resume SKIP.
    existing_commit=_read_commit(output,job_key)
    if existing_commit is not None:
        _ensure_public_commit(output,existing_commit)
    metrics = []
    verified_frames=0
    skipped_all=True
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
                frame_info=wav_info(raw)
                if record.get("seconds")!=frame_info["seconds"]:
                    raise RuntimeError("RESUME_WAV_INVALID")
                verified_frames+=frame_info["frames"]
                print(f"SKIP {order+1}/{len(sections)} {identifier} verified", flush=True)
                metrics.append({"id": identifier, "state": "SKIP", "seconds": record["seconds"]})
                continue
            raise RuntimeError("RESUME_WAV_INVALID")
        skipped_all=False
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
                     "cuda_reserved_bytes": headers.get("X-TTS-CUDA-Reserved-Bytes"),
                     "cuda_peak_bytes": headers.get("X-TTS-CUDA-Peak-Bytes"),
                     "cuda_peak_scope": headers.get("X-TTS-CUDA-Peak-Scope"),
                     "cuda_lifetime_peak_bytes": headers.get("X-TTS-CUDA-Lifetime-Peak-Bytes"),
                     "cuda_free_bytes": headers.get("X-TTS-CUDA-Free-Bytes"),
                     "speed": s.get("speed", 1.0), "voice_version": version}
            state["sections"][identifier] = entry
            verified_frames+=info["frames"]
            save()
            metrics.append({"id":identifier,**entry})
            mem = entry["cuda_peak_bytes"] or "NOT_MEASURED"
            lifetime=entry["cuda_lifetime_peak_bytes"] or "NOT_MEASURED"
            scope=entry["cuda_peak_scope"] or "NOT_MEASURED"
            print(f"PASS {identifier} wall={elapsed:.2f}s audio={info['seconds']:.2f}s rtf={entry['rtf']:.2f} "
                  f"speed={entry['speed']} pause={entry['pause_count']} peak_scope={scope} "
                  f"generation_peak_vram_bytes={mem} lifetime_peak_vram_bytes={lifetime}",
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
    if resume and skipped_all:
        prior=_existing_final_result(output,state,job_key,verified_frames,len(sections),[str(s["id"]) for s in sections])
        if prior is not None:
            assert_reference_unchanged()
            return prior
    # Consume section files lazily. Do not materialize all 200 potential 600s
    # section WAVs before enforcing the 900s final frame cap.
    def ordered_wavs():
        for section in sections:
            path=output/("section-"+str(section["id"])+".wav")
            if path.is_symlink():
                raise RuntimeError("OUTPUT_PATH_UNTRUSTED")
            yield path.read_bytes()
    # Final assembly is independently bounded to 15 minutes; no unverified WAV is published.
    final = concatenate(ordered_wavs())
    info=wav_info(final, max_bytes=FINAL_MAX_BYTES, max_seconds=FINAL_MAX_SECONDS)
    assert_reference_unchanged()
    result = {"state":"PASS","audio_s":info["seconds"],"final_sha256":info["sha256"],"sections":len(sections),
              "voice_version":version,"model_revision":MODEL_REVISION}
    return publish_final_pair(output,final,result,job_key=job_key)



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
             "REFERENCE_PROVENANCE_UNAVAILABLE","REFERENCE_CHANGED",
             "FINAL_PUBLICATION_RECOVERY_REQUIRED") else "JOB_FAILED")
        raise SystemExit(1)


if __name__=="__main__":
    main()
