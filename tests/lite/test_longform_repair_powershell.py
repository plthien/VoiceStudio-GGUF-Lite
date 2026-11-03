"""Windows PowerShell 5.1/7 behavioral replay-integrity fault injections.

Fake HTTP endpoints only. No model, voice/audio assets or NVIDIA hardware.
"""
import json
import shutil
import socket
import subprocess
import sys
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "benchmark-longform-tts.ps1"
HOSTS = ["powershell.exe", "pwsh.exe"]


def _wav():
    from io import BytesIO
    body = BytesIO()
    with wave.open(body, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(24000)
        out.writeframes(b"\\x00\\x00" * 24000)
    return body.getvalue()


@pytest.fixture
def fake_server():
    state = {"posts": 0, "mode": "ok", "restore_failure": False,
             "binary": "a" * 64, "profile_version": 3}
    audio = _wav()

    class Handler(BaseHTTPRequestHandler):
        def _json(self, value, status=200):
            body = json.dumps(value).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/profiles/ci-voice":
                self._json({"id": "ci-voice", "version": state["profile_version"],
                           "ref_sha256": "c" * 64, "source_sha256": "d" * 64})
            elif self.path == "/lite/worker/settings":
                self._json({"mode": "legacy_oneshot", "idle_timeout_seconds": 600})
            elif self.path == "/lite/worker/status":
                self._json({
                    "performance": None,
                    "runtime_binding": {
                        "schema": "lite-longform-runtime-binding-v1",
                        "status": "verified_capability",
                        "identity_sha256": state["binary"],
                        "exe_sha256": state["binary"],
                        "candidate_receipt_sha256": None,
                        "declared_backend": "Vulkan0",
                        "candidate_kind": "verified_baseline",
                        "settings_verification": "not_yet_verified",
                        "server_timeout_s": 900,
                    },
                })
            else:
                self.send_error(404)

        def do_PUT(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if state["restore_failure"] and body.get("mode") == "legacy_oneshot":
                self._json({"error": "restore rejected"}, 500)
                return
            self._json({"mode": body.get("mode")})

        def do_POST(self):
            if self.path != "/v1/audio/speech":
                self.send_error(404)
                return
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            assert payload["model"] == "omnivoice-gguf"
            state["posts"] += 1
            if state["mode"] == "drop" and state["posts"] == 1:
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.connection.close()
                return
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(audio)))
            self.send_header("X-Lite-Verification-Status", "verified")
            self.send_header("X-Lite-Verified-Backend",
                             "CPU" if state["mode"] == "forged" else "Vulkan0")
            self.send_header("X-Lite-Observed-Device", "NVIDIA GeForce GTX 1650")
            self.send_header("X-Lite-Effective-Speed", "1.0")
            self.send_header("X-Lite-Audio-S", "1.0")
            self.send_header("X-Lite-RTF", "1.0")
            self.end_headers()
            self.wfile.write(audio)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def _run(host, port, sections_file, output, *extra):
    executable = shutil.which(host)
    if not executable:
        pytest.fail(f"required PowerShell host missing: {host}")
    args = [executable, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(SCRIPT), "-Voice", "ci-voice", "-SectionsFile",
            str(sections_file), "-Output", str(output), "-Port", str(port),
            "-VoiceVersion", "3", *map(str, extra)]
    return subprocess.run(args, cwd=ROOT, text=True, capture_output=True, timeout=55)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell only")
@pytest.mark.parametrize("host", HOSTS)
def test_uncertain_post_requires_explicit_section_retry(host, fake_server, tmp_path):
    state, port = fake_server
    sections = tmp_path / "sections.json"
    sections.write_text(json.dumps(["one sentence."]), encoding="utf-8")
    output = tmp_path / "output"
    state["mode"] = "drop"
    first = _run(host, port, sections, output)
    assert first.returncode != 0
    assert state["posts"] == 1
    run = next(output.iterdir())
    journal = run / "section-0000.wav.attempt.json"
    assert json.loads(journal.read_text(encoding="utf-8"))["state"] == "IN_FLIGHT"

    state["mode"] = "ok"
    resumed = _run(host, port, sections, output, "-RunDir", run)
    assert resumed.returncode != 0
    assert "UNCERTAIN_SECTION_REQUIRES_OWNER_DECISION" in resumed.stdout + resumed.stderr
    assert state["posts"] == 1

    approved = _run(host, port, sections, output, "-RunDir", run,
                    "-RetryUncertainSections", "0")
    assert approved.returncode == 0, approved.stdout + approved.stderr
    assert state["posts"] == 2
    assert json.loads(journal.read_text(encoding="utf-8"))["state"] == "VERIFIED_COMMITTED"

    repeat = _run(host, port, sections, output, "-RunDir", run)
    assert repeat.returncode == 0, repeat.stdout + repeat.stderr
    assert state["posts"] == 2
    assert "PASS resume section=0" in repeat.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell only")
@pytest.mark.parametrize("host", HOSTS)
def test_runtime_binding_change_refuses_before_post(host, fake_server, tmp_path):
    state, port = fake_server
    sections = tmp_path / "sections.json"
    sections.write_text(json.dumps(["one sentence."]), encoding="utf-8")
    output = tmp_path / "output"
    first = _run(host, port, sections, output)
    assert first.returncode == 0, first.stdout + first.stderr
    assert state["posts"] == 1
    state["binary"] = "b" * 64
    second = _run(host, port, sections, output, "-RunDir", next(output.iterdir()))
    assert second.returncode != 0
    assert state["posts"] == 1


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell only")
@pytest.mark.parametrize("host", HOSTS)
def test_restore_500_forces_fail_report_no_terminal_pass(host, fake_server, tmp_path):
    state, port = fake_server
    sections = tmp_path / "sections.json"
    sections.write_text(json.dumps(["one sentence."]), encoding="utf-8")
    output = tmp_path / "output"
    state["restore_failure"] = True
    run_result = _run(host, port, sections, output)
    assert run_result.returncode != 0
    assert "SETTINGS_RESTORE_FAILED" in run_result.stdout
    assert "PASS longform" not in run_result.stdout
    report = json.loads((next(output.iterdir()) / "metrics.json").read_text(encoding="utf-8-sig"))
    assert report["overall_status"] == "FAIL"
    assert report["restore_status"] == "FAIL"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell only")
@pytest.mark.parametrize("host", HOSTS)
def test_forged_backend_prevents_commit_and_default_replay(host, fake_server, tmp_path):
    state, port = fake_server
    sections = tmp_path / "sections.json"
    sections.write_text(json.dumps(["one sentence."]), encoding="utf-8")
    output = tmp_path / "output"
    state["mode"] = "forged"
    first = _run(host, port, sections, output)
    assert first.returncode != 0
    assert state["posts"] == 1
    run = next(output.iterdir())
    assert not list(run.glob("section-*.wav.json"))
    assert not (run / "final.wav").exists()
    state["mode"] = "ok"
    resumed = _run(host, port, sections, output, "-RunDir", run)
    assert resumed.returncode != 0
    assert state["posts"] == 1
