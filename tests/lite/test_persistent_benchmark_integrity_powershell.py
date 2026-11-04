import json
import os
import shutil
import subprocess
import sys
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "benchmark-persistent-worker.ps1"
HOSTS = ["powershell.exe", "pwsh.exe"]


def _wav():
    stream = BytesIO()
    with wave.open(stream, "wb") as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(24000)
        out.writeframes(b"\0\0" * 240)
    return stream.getvalue()


def _host(name):
    if sys.platform != "win32":
        pytest.skip("actual Windows PowerShell hosts only")
    executable = shutil.which(name)
    if not executable:
        pytest.fail(f"required PowerShell host missing: {name}")
    try:
        probe = subprocess.run([executable, "-NoProfile", "-NonInteractive", "-Command",
                                "$PSVersionTable.PSVersion.ToString()"],
                               capture_output=True, text=True, timeout=10)
    except OSError as exc:
        pytest.skip(f"PowerShell app alias is not executable locally: {exc}")
    assert probe.returncode == 0, probe.stdout + probe.stderr
    return executable


@pytest.fixture
def benchmark_server():
    state = {
        "mode": "matching", "posts": [], "request_id": None, "generation": 1,
        "worker_mode": "legacy_oneshot", "idle": 600, "puts": [],
        "profile_calls": {"private-a": 0, "private-b": 0},
        "profiles": {
            "private-a": {"id": "private-a", "version": 1,
                          "ref_sha256": "a" * 64, "source_sha256": "b" * 64},
            "private-b": {"id": "private-b", "version": 1,
                          "ref_sha256": "c" * 64, "source_sha256": "d" * 64},
        },
        "mutate_on_get": {}, "mid_mutation": None, "missing_profile": None,
        "malformed_profile": None,
    }
    audio = _wav()

    class Handler(BaseHTTPRequestHandler):
        def _json(self, value, code=200):
            body = json.dumps(value).encode()
            self.send_response(code); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/lite/worker/settings":
                self._json({"mode": state["worker_mode"], "idle_timeout_seconds": state["idle"]})
                return
            if self.path.startswith("/profiles/"):
                voice = unquote(self.path.split("/profiles/", 1)[1])
                if voice == state["missing_profile"]:
                    self._json({"detail": "missing"}, 404); return
                state["profile_calls"][voice] += 1
                mutation = state["mutate_on_get"].get((voice, state["profile_calls"][voice]))
                if mutation:
                    state["profiles"][voice].update(mutation)
                value = dict(state["profiles"][voice])
                if voice == state["malformed_profile"]:
                    value["ref_sha256"] = "not-a-hash"
                self._json(value); return
            if self.path == "/lite/worker/status":
                candidate = state["mode"] not in {"baseline", "invalid-status"}
                request_id = state["request_id"]
                completed = request_id
                completed_generation = state["generation"]
                if state["mode"] == "race" and request_id:
                    completed = "another-request"
                if state["mode"] == "generation" and request_id:
                    completed_generation = state["generation"] - 1
                perf = ({"native_total_us": 101, "ref_preprocess_us": 2,
                         "ref_codec_encode_us": 3, "maskgit_lm_forward_us": 4,
                         "maskgit_non_lm_overhead_us": 5, "codec_decode_us": 6,
                         "postprocess_us": 7, "ref_cache_hit": 1, "maskgit_steps": 32}
                        if candidate else None)
                self._json({
                    "state": "READY", "resident": True, "pid": 4321,
                    "worker_generation": state["generation"], "cold_load_count": 1,
                    "synthesis_count": len(state["posts"]),
                    "reuse_count": max(0, len(state["posts"]) - 1),
                    "last_completed_request_id": completed,
                    "last_completed_worker_generation": completed_generation,
                    "performance_status": ("VERIFIED" if candidate else
                                           "BROKEN" if state["mode"] == "invalid-status" else "NOT_AVAILABLE"),
                    "performance": perf,
                    "runtime_binding": {"candidate_kind": "verified_candidate" if candidate else "verified_baseline"},
                }); return
            self.send_error(404)

        def do_PUT(self):
            value = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            state["puts"].append(value)
            state["worker_mode"] = value["mode"]
            state["idle"] = value.get("idle_timeout_seconds")
            self._json(value)

        def do_POST(self):
            if self.path == "/lite/worker/unload":
                self._json({"status": "ok"}); return
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            state["posts"].append(payload)
            state["request_id"] = f"request-{len(state['posts'])}"
            if state["mid_mutation"]:
                voice, change = state["mid_mutation"]
                if payload["voice"] == voice:
                    state["profiles"][voice].update(change)
                    state["mid_mutation"] = None
            self.send_response(200); self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(audio)))
            self.send_header("X-Lite-Verification-Status", "verified")
            self.send_header("X-Lite-Audio-S", "0.01"); self.send_header("X-Lite-RTF", "1")
            if state["mode"] != "missing-id":
                self.send_header("X-Lite-Request-Id", state["request_id"])
            self.end_headers(); self.wfile.write(audio)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        yield state, server.server_port
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


def _run(host, port, output, *cases):
    executable = _host(host)
    args = [executable, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(SCRIPT), "-VoiceA", "private-a", "-Output", str(output),
            "-Port", str(port)]
    if "aba" in cases:
        args += ["-VoiceB", "private-b"]
    if cases:
        args += ["-Cases", ",".join(cases)]
    return subprocess.run(args, cwd=ROOT, env={**os.environ, "CI": "true"},
                          capture_output=True, text=True, timeout=30)


def _metrics(output):
    run = next(output.iterdir())
    return json.loads((run / "owner-metrics.json").read_text(encoding="utf-8-sig")), run


@pytest.mark.parametrize("host", HOSTS)
@pytest.mark.parametrize("mode,code", [
    ("matching", None), ("race", "PERF_NOT_ATTRIBUTABLE"),
    ("generation", "PERF_NOT_ATTRIBUTABLE"),
    ("missing-id", "REQUEST_CORRELATION_MISSING"), ("baseline", None),
    ("invalid-status", "PERF_NOT_ATTRIBUTABLE"),
])
def test_persistent_perf_is_request_owned_or_explicitly_unavailable(
        host, mode, code, benchmark_server, tmp_path):
    state, port = benchmark_server; state["mode"] = mode
    result = _run(host, port, tmp_path / mode, "cold")
    report, run = _metrics(tmp_path / mode)
    row = report["rows"][0]
    if code:
        assert result.returncode != 0
        assert row["state"] == "FAIL" and row["error"] == code
        assert "perf_native_total_us" not in row
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert row["state"] == "READY"
        if mode == "matching":
            assert row["performance_status"] == "VERIFIED"
            assert row["perf_native_total_us"] == 101
        else:
            assert row["performance_status"] == "NOT_AVAILABLE"
            assert row["perf_native_total_us"] is None
    exported = (run / "owner-metrics.json").read_text() + (run / "owner-metrics.csv").read_text()
    assert "request-1" not in exported and "another-request" not in exported
    public_output = result.stdout + result.stderr
    assert "private-a" not in public_output and "Persistent worker comparable request." not in public_output
    assert len(state["posts"]) == 1
    assert state["worker_mode"] == "legacy_oneshot" and state["idle"] == 600


@pytest.mark.parametrize("host", HOSTS)
def test_clean_run_after_perf_race_is_not_contaminated(host, benchmark_server, tmp_path):
    state, port = benchmark_server; state["mode"] = "race"
    failed = _run(host, port, tmp_path / "raced", "cold")
    assert failed.returncode != 0
    state["mode"] = "matching"
    clean = _run(host, port, tmp_path / "clean", "cold")
    report, _ = _metrics(tmp_path / "clean")
    assert clean.returncode == 0, clean.stdout + clean.stderr
    assert report["rows"][0]["performance_status"] == "VERIFIED"
    assert report["rows"][0]["perf_native_total_us"] == 101


@pytest.mark.parametrize("host", HOSTS)
@pytest.mark.parametrize("scenario", ["stable-warm", "drift-before", "drift-mid",
                                        "stable-aba", "drift-b", "missing", "malformed",
                                        "malformed-version"])
def test_persistent_comparisons_freeze_voice_identity(
        host, scenario, benchmark_server, tmp_path):
    state, port = benchmark_server; state["mode"] = "baseline"
    cases = ("cold", "warm")
    if scenario == "drift-before":
        state["mutate_on_get"][("private-a", 4)] = {"version": 2}
    elif scenario == "drift-mid":
        state["mid_mutation"] = ("private-a", {"ref_sha256": "e" * 64})
        cases = ("cold",)
    elif scenario == "stable-aba":
        cases = ("aba",)
    elif scenario == "drift-b":
        state["mutate_on_get"][("private-b", 2)] = {"source_sha256": "e" * 64}
        cases = ("aba",)
    elif scenario == "missing":
        state["missing_profile"] = "private-a"; cases = ("cold",)
    elif scenario == "malformed":
        state["malformed_profile"] = "private-a"; cases = ("cold",)
    elif scenario == "malformed-version":
        state["profiles"]["private-a"]["version"] = "not-an-integer"; cases = ("cold",)
    result = _run(host, port, tmp_path / scenario, *cases)
    report, run = _metrics(tmp_path / scenario)
    if scenario == "stable-warm":
        assert result.returncode == 0, result.stdout + result.stderr
        assert len(state["posts"]) == 3
        assert all(p["voice"] == "private-a" and p["seed"] == 42 and
                   p["speed"] == 1.0 and p["input"] == "Persistent worker comparable request."
                   for p in state["posts"])
    elif scenario == "stable-aba":
        assert result.returncode == 0, result.stdout + result.stderr
        assert [p["voice"] for p in state["posts"]] == ["private-a", "private-b", "private-a"]
    elif scenario == "drift-before":
        assert result.returncode != 0 and len(state["posts"]) == 1
        assert report["rows"][-1]["error"] == "PROFILE_BINDING_CHANGED"
    elif scenario == "drift-mid":
        assert result.returncode != 0 and len(state["posts"]) == 1
        assert report["rows"][0]["error"] == "PROFILE_BINDING_CHANGED"
    elif scenario == "drift-b":
        assert result.returncode != 0 and [p["voice"] for p in state["posts"]] == ["private-a"]
        assert report["rows"][-1]["error"] == "PROFILE_BINDING_CHANGED"
    else:
        assert result.returncode != 0 and not state["posts"]
        assert "PROFILE_BINDING_UNAVAILABLE" in result.stdout
    exported = (run / "owner-metrics.json").read_text() + (run / "owner-metrics.csv").read_text()
    public_output = result.stdout + result.stderr
    for private in ("private-a", "private-b", "Persistent worker comparable request.",
                    "a" * 64, "b" * 64, "c" * 64, "d" * 64):
        assert private not in exported
        assert private not in public_output
    assert state["worker_mode"] == "legacy_oneshot" and state["idle"] == 600
