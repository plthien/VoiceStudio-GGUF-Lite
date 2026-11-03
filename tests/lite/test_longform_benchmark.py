import io,json,shutil,subprocess,sys,threading,time,wave
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import pytest
from lite.longform_benchmark import (MAX_CHARS,MAX_PAUSES,MAX_ESTIMATED_SECONDS,
    assemble_wavs,create_manifest,estimated_seconds,metrics_row,pause_count,
    speech_seconds,split_section,write_manifest_atomic)

BINDING="a"*64

def test_lossless_bounded_section_split_and_manifest(tmp_path):
    text=("A complete sentence with words. "*700)+"Final word."
    parts=split_section(text)
    assert "".join(x.text for x in parts)==text
    assert all(len(x.text)<=MAX_CHARS for x in parts)
    assert all(pause_count(x.text)<=MAX_PAUSES for x in parts)
    assert all(estimated_seconds(x.text)<=MAX_ESTIMATED_SECONDS for x in parts)
    manifest=create_manifest([text],voice_version=7,profile_binding_sha256=BINDING,
                             seed=42,speed=1.0)
    path=tmp_path/"manifest.json";write_manifest_atomic(path,manifest);write_manifest_atomic(path,manifest)
    changed={**manifest,"speed":1.1}
    with pytest.raises(ValueError):write_manifest_atomic(path,changed)

def test_fake_600_second_streaming_assembly_exact(tmp_path):
    paths=[]
    for index in range(10):
        path=tmp_path/f"{index}.wav";paths.append(path)
        with wave.open(str(path),"wb") as out:
            out.setnchannels(1);out.setsampwidth(2);out.setframerate(24000)
            block=b"\x01\x00"*24000
            for _ in range(60):out.writeframesraw(block)
    result=assemble_wavs(paths,tmp_path/"final.wav")
    assert result["frames"]==600*24000 and result["audio_s"]==600
    with wave.open(str(tmp_path/"final.wav"),"rb") as src:assert src.getnframes()==600*24000

def test_pause_does_not_mask_speech_rtf():
    row=metrics_row(index=0,wall_s=30,audio_s=20,speech_s=10,cold=True,
                    headers={"X-Lite-Verification-Status":"verified"})
    assert row["final_rtf"]==1.5 and row["speech_rtf"]==3

def test_speech_seconds_uses_verified_audio_for_non_pause_response():
    assert speech_seconds({"X-Lite-Audio-S":"12.5"},12.5)==12.5

def test_speech_seconds_uses_pause_response_header_case_insensitively():
    assert speech_seconds({"x-lite-speech-s":"9.25"},12.5)==9.25

def test_canonical_default_and_spaced_pause_markers_split_safely():
    text=("speech [pause] "*17)+"tail [ pause 800 ms ] end"
    parts=split_section(text)
    assert "".join(x.text for x in parts)==text
    assert sum(pause_count(x.text) for x in parts)==18
    assert all(pause_count(x.text)<=MAX_PAUSES for x in parts)
    assert estimated_seconds("a [pause] b [ pause 800 ms ] c")==pytest.approx(2.35)

@pytest.mark.parametrize("token",["x"*4097,"[pause 1s] "*17+"end"])
def test_constraints_force_safe_split_or_reject(token):
    try: parts=split_section(token)
    except ValueError:return
    assert "".join(x.text for x in parts)==token
    assert all(len(x.text)<=4096 and pause_count(x.text)<=16 for x in parts)

def test_profile_binding_is_immutable_and_opaque(tmp_path):
    first=create_manifest(["hello"],voice_version=3,profile_binding_sha256=BINDING)
    assert first["profile_binding_sha256"]==BINDING
    assert "private-voice-id" not in json.dumps(first)
    path=tmp_path/"manifest.json";write_manifest_atomic(path,first)
    changed=create_manifest(["hello"],voice_version=4,profile_binding_sha256="b"*64)
    with pytest.raises(ValueError,match="resume manifest differs"):
        write_manifest_atomic(path,changed)

def test_powershell_resume_speech_fallback_and_profile_guards_are_executable_contracts():
    script=(Path(__file__).parents[2]/"scripts"/"benchmark-longform-tts.ps1").read_text()
    assert '[string]$RunDir=""' in script
    assert '$qualified=(-not $resuming' in script
    assert 'if([string]::IsNullOrWhiteSpace($speechHeader)){$audioSeconds}' in script
    assert script.count("Assert-ProfileUnchanged $profile")>=3
    assert 'profile_binding_sha256=$profileBinding' in script
    assert "function Get-FileSha256" in script and "Get-FileHash" not in script

@pytest.mark.skipif(sys.platform!="win32",reason="actual Windows PowerShell regression")
def test_powershell_resume_and_speech_headers_end_to_end(tmp_path):
    executable=shutil.which("powershell.exe")
    assert executable
    buffer=io.BytesIO()
    with wave.open(buffer,"wb") as out:
        out.setnchannels(1);out.setsampwidth(2);out.setframerate(24000)
        out.writeframes(b"\0\0"*24000)
    wav_bytes=buffer.getvalue();posts=[]
    profile={"id":"private-voice","version":4,"ref_sha256":"a"*64,
             "source_sha256":"b"*64}

    class Handler(BaseHTTPRequestHandler):
        def _json(self,value):
            body=json.dumps(value).encode();self.send_response(200)
            self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",str(len(body)));self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            if self.path=="/profiles/private-voice":self._json(profile)
            elif self.path=="/lite/worker/settings":self._json({"mode":"legacy","idle_timeout_seconds":600})
            elif self.path=="/lite/worker/status":self._json({"performance":{"native_total_us":1,"ref_codec_encode_us":2,"ref_cache_hit":False,"maskgit_steps":32}})
            else:self.send_error(404)
        def do_PUT(self):
            self.rfile.read(int(self.headers.get("Content-Length","0")))
            self._json({"status":"ok"})
        def do_POST(self):
            payload=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            posts.append(payload);self.send_response(200)
            self.send_header("Content-Type","audio/wav")
            self.send_header("Content-Length",str(len(wav_bytes)))
            self.send_header("X-Lite-Verification-Status","verified")
            self.send_header("X-Lite-Audio-S","1.0")
            self.send_header("X-Lite-RTF","0.1")
            if "[pause]" in payload["input"]:
                self.send_header("X-Lite-Speech-S","0.5")
            self.end_headers();self.wfile.write(wav_bytes)
        def log_message(self,*_):pass

    sections=tmp_path/"sections.json"
    sections.write_text(json.dumps(["plain narration","one [pause] two"]),encoding="utf-8")
    output=tmp_path/"runs";script=Path(__file__).parents[2]/"scripts"/"benchmark-longform-tts.ps1"
    server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=[executable,"-NoProfile","-NonInteractive","-ExecutionPolicy","Bypass","-File",str(script),
          "-Voice","private-voice","-SectionsFile",str(sections),"-Output",str(output),
          "-Port",str(server.server_port),"-VoiceVersion","4"]
    try:
        first=subprocess.run(base,cwd=Path(__file__).parents[2],capture_output=True,text=True,timeout=30)
        run=next(output.iterdir())
        first_metrics=json.loads((run/"metrics.json").read_text(encoding="utf-8-sig"))
        second=subprocess.run(base+["-RunDir",str(run)],cwd=Path(__file__).parents[2],
                              capture_output=True,text=True,timeout=30)
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
    assert first.returncode==0,first.stdout+first.stderr
    assert second.returncode==0,second.stdout+second.stderr
    assert first_metrics["resumed_run"] is False and first_metrics["qualified_fresh_run"] is True
    assert len(posts)==2 and second.stdout.count("PASS resume section=")==2
    fresh=json.loads((run/"metrics.json").read_text(encoding="utf-8-sig"))
    assert fresh["resumed_run"] is True and fresh["qualified_fresh_run"] is False
    assert [row["speech_s"] for row in fresh["sections"]]==[1.0,0.5]

def test_inspect_wav_is_bounded_and_rejects_corruption(tmp_path):
    import tracemalloc
    from lite.longform_benchmark import inspect_wav
    wav = tmp_path / "section.wav"
    with wave.open(str(wav), "wb") as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(24000)
        for _ in range(60):
            out.writeframesraw(b"\\0\\0" * 24000)
    tracemalloc.start()
    result = inspect_wav(wav)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert result["frames"] == 60 * 24000
    assert peak < 1024 * 1024  # Not a complete 2.88 MB PCM allocation.
    with wav.open("r+b") as fh:
        fh.truncate(fh.seek(0, 2) - 100)
    with pytest.raises(ValueError, match="truncated"):
        inspect_wav(wav)


def test_metrics_rows_are_durable_and_sparse_failures_supported(tmp_path):
    from lite.longform_benchmark import write_metrics
    started = time.monotonic()
    write_metrics(tmp_path, [{"index": 0, "audio_s": 2, "speech_s": 2,
                              "status": "PASS"}], started)
    report = write_metrics(tmp_path, [{"index": 0, "audio_s": 2, "speech_s": 2,
                                        "status": "PASS"},
                                       {"index": 1, "status": "FAIL",
                                        "error": "NATIVE_TIMEOUT"}], started)
    assert len(report["sections"]) == 2
    assert json.loads((tmp_path / "metrics.json").read_text())["sections"][1]["status"] == "FAIL"
    assert not list(tmp_path.glob("*.partial"))
