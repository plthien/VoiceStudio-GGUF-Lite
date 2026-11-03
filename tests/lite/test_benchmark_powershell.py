import json
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "benchmark-persistent-worker.ps1"


@pytest.mark.skipif(sys.platform != "win32", reason="actual Windows PowerShell hosts only")
@pytest.mark.parametrize("host", ["powershell.exe", "pwsh.exe"],
                         ids=["WindowsPowerShell5_1", "PowerShell7"])
def test_benchmark_http_helper_is_noninteractive_and_private(host):
    executable = shutil.which(host)
    if not executable:
        pytest.fail(f"required PowerShell host missing: {host}")
    try:
        probe = subprocess.run([executable, "-NoProfile", "-NonInteractive", "-Command",
                                "$PSVersionTable.PSVersion.ToString()"],
                               capture_output=True, text=True, timeout=10)
    except OSError as exc:
        pytest.skip(f"PowerShell app alias is not executable in this local sandbox: {exc}")
    assert probe.returncode == 0, probe.stdout + probe.stderr
    observed = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            observed.append(json.loads(body))
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("X-Lite-Verification-Status", "verified")
            self.send_header("Content-Length", "64")
            self.end_headers()
            self.wfile.write(b"RIFF" + b"\0" * 60)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/v1/audio/speech"
        result = subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(SCRIPT), "-CiMockSpeechUrl", url],
            capture_output=True, text=True, timeout=20,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "BENCHMARK_HTTP_SMOKE_PASS"
    assert observed == [{"input": "CI smoke", "model": "omnivoice-gguf",
                         "response_format": "wav", "seed": 42,
                         "speed": 1.0, "voice": "ci-smoke"}]
