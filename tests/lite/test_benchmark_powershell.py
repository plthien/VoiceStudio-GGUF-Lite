import json
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import wave
from io import BytesIO


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
            if self.path.endswith("/fail"):
                self.send_response(500);self.end_headers();return
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
        skipped=subprocess.run([executable,"-NoProfile","-NonInteractive","-ExecutionPolicy","Bypass",
            "-File",str(SCRIPT),"-CiMockSpeechUrl",url,"-Cases","different"],capture_output=True,text=True,timeout=20)
        failed=subprocess.run([executable,"-NoProfile","-NonInteractive","-ExecutionPolicy","Bypass",
            "-File",str(SCRIPT),"-CiMockSpeechUrl",url+"/fail","-ContinueOnFailure"],capture_output=True,text=True,timeout=20)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "START case=ci-http" in result.stdout
    assert "PASS case=ci-http" in result.stdout
    assert "PASS settings-restore unchanged" in result.stdout
    assert result.stdout.strip().endswith("BENCHMARK_HTTP_SMOKE_PASS")
    assert len(observed)==2 and observed[0] == {"input": "CI smoke", "model": "omnivoice-gguf",
                         "response_format": "wav", "seed": 42,
                         "speed": 1.0, "voice": "ci-smoke"}
    assert skipped.returncode!=0 and "UNKNOWN_CASE_SELECTOR: different" in (skipped.stdout+skipped.stderr)
    assert failed.returncode==2 and "START case=ci-http" in failed.stdout and "FAIL case=ci-http" in failed.stdout
    assert "PASS settings-restore unchanged" in failed.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="actual Windows PowerShell hosts only")
def test_idle_group_executes_never_start_before_measurement(tmp_path):
    executable=shutil.which("powershell.exe");assert executable
    audio=BytesIO()
    with wave.open(audio,"wb") as out:
        out.setnchannels(1);out.setsampwidth(2);out.setframerate(24000)
        out.writeframes(b"\0\0"*240)
    wav=audio.getvalue();state={"resident":False,"pid":None,"generation":0,
                                "idle":600,"mode":"legacy_oneshot","posts":[]}
    class Handler(BaseHTTPRequestHandler):
        def _json(self,value):
            body=json.dumps(value).encode();self.send_response(200)
            self.send_header("Content-Type","application/json");self.send_header("Content-Length",str(len(body)))
            self.end_headers();self.wfile.write(body)
        def do_GET(self):
            if self.path=="/lite/worker/settings":self._json({"mode":state["mode"],"idle_timeout_seconds":state["idle"]})
            elif self.path=="/profiles/a":self._json({"id":"a","version":1,"ref_sha256":"a"*64,"source_sha256":"b"*64})
            elif self.path=="/lite/worker/status":self._json({"state":"READY" if state["resident"] else "UNLOADED","resident":state["resident"],"pid":state["pid"],"worker_generation":state["generation"],"cold_load_count":state["generation"],"synthesis_count":len(state["posts"]),"reuse_count":max(0,len(state["posts"])-1),"performance":None,"performance_status":"NOT_AVAILABLE"})
            else:self.send_error(404)
        def do_PUT(self):
            value=json.loads(self.rfile.read(int(self.headers.get("Content-Length","0"))))
            state["mode"]=value["mode"];state["idle"]=value.get("idle_timeout_seconds");self._json(value)
        def do_POST(self):
            if self.path=="/lite/worker/unload":
                state["resident"]=False;state["pid"]=None;self._json({"status":"ok"});return
            payload=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["posts"].append(payload["input"])
            if not state["resident"]:state["generation"]+=1;state["pid"]=4321
            state["resident"]=True;self.send_response(200)
            self.send_header("Content-Length",str(len(wav)));self.send_header("X-Lite-Verification-Status","verified")
            self.send_header("X-Lite-Audio-S","0.01");self.send_header("X-Lite-RTF","1")
            self.end_headers();self.wfile.write(wav)
        def log_message(self,*_):pass
    server=ThreadingHTTPServer(("127.0.0.1",0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    env={**__import__('os').environ,"CI":"true"}
    try:
        result=subprocess.run([executable,"-NoProfile","-NonInteractive","-ExecutionPolicy","Bypass","-File",str(ROOT/"scripts"/"benchmark-persistent-worker.ps1"),"-VoiceA","a","-VoiceB","b","-Output",str(tmp_path/"out"),"-Port",str(server.server_port),"-Cases","idle","-IdleObservationSeconds","1"],cwd=ROOT,env=env,capture_output=True,text=True,timeout=25)
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
    assert result.returncode==0,result.stdout+result.stderr
    assert state["posts"]==["Idle unload precondition request.","Never idle policy request."]
    assert "PASS case=idle-start" in result.stdout and "PASS case=never-start" in result.stdout
