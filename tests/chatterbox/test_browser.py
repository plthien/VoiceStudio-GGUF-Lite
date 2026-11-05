"""Rendered Chromium behavioral tests: real DOM, API, and fake CUDA worker."""
from __future__ import annotations
import socket
import re
import json
import httpx
import threading
import time

import pytest
import uvicorn
from playwright.sync_api import sync_playwright, expect

from lite.chatterbox_app import create_app
from lite.audio import wav_info
from lite.chatterbox_config import ChatterboxSettings
from .test_contracts import FakeTurbo, audio


@pytest.fixture
def live(tmp_path):
    import sqlite3
    settings = ChatterboxSettings(root=tmp_path, timeout=15)
    fake = FakeTurbo(settings)
    application = create_app(settings=settings, service=fake)
    sock=socket.socket()
    sock.bind(("127.0.0.1",0))
    port=sock.getsockname()[1]
    sock.close()
    config=uvicorn.Config(application,host="127.0.0.1",port=port,log_level="error",
                         access_log=False,lifespan="on")
    server=uvicorn.Server(config)
    thread=threading.Thread(target=server.run,daemon=True)
    thread.start()
    for attempt in range(100):
        if server.started: break
        time.sleep(.04)
    assert server.started
    try: yield "http://127.0.0.1:"+str(port),fake
    finally:
        server.should_exit=True
        thread.join(10)


@pytest.mark.parametrize("viewport",[(1280,900),(390,844)])
def test_cloning_generate_library_and_runtime(live, viewport):
    url, fake=live
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True)
        page=browser.new_page(viewport={"width":viewport[0],"height":viewport[1]})
        page.goto(url+"/ui")
        expect(page.get_by_role("heading",name="VoiceStudio Nano Lite")).to_be_visible()
        page.get_by_label("Voice name").fill("Owned Voice")
        page.get_by_label("Exact transcript",exact=True).fill("This is the exact audio transcript")
        page.locator('#clone-form input[name=ref_audio]').set_input_files({
          "name":"owned.wav","mimeType":"audio/wav","buffer":audio(6)})
        page.locator("#consent").check()
        page.get_by_role("button",name="Create voice").click()
        expect(page.get_by_text("Owned Voice · version 1")).to_be_visible(timeout=10000)
        page.get_by_role("button",name="Preview",exact=True).click()
        page.get_by_role("tab",name="Text to Speech").click()
        page.locator("#input").fill("Hello. [pause 250ms] Goodbye.")
        page.locator("#speed").evaluate("(el) => { el.value = '1.15'; el.dispatchEvent(new Event('input', {bubbles:true})); }")
        page.get_by_role("button",name="Generate WAV").click()
        expect(page.get_by_text("Generation complete")).to_be_visible(timeout=15000)
        expect(page.locator("#download")).to_be_visible()
        expect(page.locator("#metrics")).to_contain_text("Speech WPM")
        page.get_by_role("tab",name="Runtime & Settings").click()
        expect(page.locator("#runtime-status")).to_contain_text("FAKE CUDA TEST ONLY")
        page.locator("#idle").select_option("300")
        page.get_by_role("button",name="Save setting").click()
        expect(page.get_by_text("Idle setting saved")).to_be_visible()
        page.get_by_role("button",name="Unload Now").click()
        expect(page.get_by_text("Worker unloaded")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        browser.close()


def test_error_and_cancel_controls(live):
    url, fake=live
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True)
        page=browser.new_page()
        page.goto(url+"/ui")
        page.get_by_role("tab",name="Text to Speech").click()
        expect(page.locator("#generate")).to_be_disabled()
        page.get_by_role("tab",name="Voice Cloning").click()
        page.locator('#clone-form input[name=name]').fill("Own")
        page.locator('#clone-form textarea[name=ref_text]').fill("Exact transcript")
        page.locator('#clone-form input[name=ref_audio]').set_input_files({
            "name":"owned.wav","mimeType":"audio/wav","buffer":audio(6)})
        page.locator("#consent").check()
        page.locator("#clone-form button[type=submit]").click()
        expect(page.get_by_text("Own · version 1")).to_be_visible(timeout=10000)
        fake.fail=True
        page.get_by_role("tab",name="Text to Speech").click()
        page.locator("#input").fill("Hello.")
        page.locator("#generate").click()
        expect(page.locator("#notice")).to_contain_text("WORKER_FAILURE",timeout=15000)
        fake.fail=False
        fake.delaying=True
        page.locator("#generate").click()
        expect(page.locator("#cancel")).to_be_enabled()
        page.locator("#cancel").click()
        expect(page.locator("#notice")).to_contain_text(re.compile("CANCELLED|Cancellation requested"))
        browser.close()


def test_longform_http_resume_end_to_end(live, tmp_path):
    """Actual HTTP generation/atomic WAV, cache resume and version refusal."""
    from lite.chatterbox_assembly import do_job
    url, fake = live
    with httpx.Client(timeout=20) as client:
        r = client.post(url + "/profiles",
                        data={"name": "Owned", "ref_text": "Exact words from reference",
                              "language": "en", "consent": "confirmed"},
                        files={"ref_audio": ("owned.wav", audio(6), "audio/wav")})
        assert r.status_code == 200, r.text
        voice = r.json()["id"]
        manifest = tmp_path / "owner-input.json"
        manifest.write_text(json.dumps({"voice": voice, "sections": [
            {"id": "sec_001", "input": "One sentence."},
            {"id": "sec_002", "input": "Second sentence. [pause 250ms] Continue."}
        ]}), encoding="utf8")
        target = tmp_path / "owned-output"
        result = do_job(manifest, target, base=url)
        assert result["state"] == "PASS" and result["sections"] == 2
        state = json.loads((target / "resume-manifest.json").read_text())
        assert state["sections"]["sec_001"]["cuda_peak_bytes"] == "2000"
        assert state["sections"]["sec_001"]["backend"] == "CUDA0"
        assert abs(result["audio_s"] - 1.0) < .001
        before = fake.syntheses
        reversed_manifest=tmp_path/"reverse-order.json"
        reversed_manifest.write_text(json.dumps({"voice":voice,"sections":[
            {"id":"sec_002","input":"Second sentence. [pause 250ms] Continue."},
            {"id":"sec_001","input":"One sentence."}]}))
        with pytest.raises(RuntimeError, match="RESUME_PROVENANCE_CHANGED"):
            do_job(reversed_manifest,target,base=url,resume=True)
        result = do_job(manifest, target, base=url, resume=True)
        assert fake.syntheses == before
        assert wav_info((target / "final.wav").read_bytes())["frames"] == 24000
        changed = target / "section-sec_002.wav"
        original = changed.read_bytes()
        changed.write_bytes(audio(.8))
        with pytest.raises(RuntimeError, match="RESUME_WAV_INVALID"):
            do_job(manifest, target, base=url, resume=True)
        changed.write_bytes(original)
        newer = client.put(url + "/profiles/" + voice + "/audio",
                           data={"ref_text": "Changed words", "consent": "confirmed"},
                           files={"ref_audio": ("updated.wav", audio(6.3), "audio/wav")})
        assert newer.status_code == 200
        with pytest.raises(RuntimeError, match="RESUME_PROVENANCE_CHANGED"):
            do_job(manifest, target, base=url, resume=True)



def test_benchmark_longform_forces_cold_start(live, tmp_path, monkeypatch):
    """A warm Smoke run must not exclude cold model load from LongForm RTF."""
    import sys
    from lite.chatterbox_benchmark import main as benchmark_main
    url, fake = live
    with httpx.Client(timeout=20) as client:
        response=client.post(url+"/profiles",
                   data={"name":"Owned","ref_text":"Original audio words", "language":"en",
                         "consent":"confirmed"},
                   files={"ref_audio":("owned.wav",audio(6),"audio/wav")})
        assert response.status_code==200
        voice=response.json()["id"]
        warm=client.post(url+"/v1/audio/speech",json={
            "model":"chatterbox-nano", "voice":voice, "input":"Warm the model.",
            "response_format":"wav", "speed":1.0, "language":"en"})
        assert warm.status_code==200
        assert fake.loads==1
        manifest=tmp_path/"narration.json"
        manifest.write_text(json.dumps({"voice":voice,"sections":[
            {"id":"sec_001","input":"This is a separate cold-start narration."}
        ]}),encoding="utf8")
        output=tmp_path/"benchmark"
        monkeypatch.setattr(sys,"argv",["benchmark","--mode","LongForm",
                    "--voice-id",voice,"--manifest",str(manifest),
                    "--output",str(output),"--base",url])
        assert benchmark_main()==1  # Short operational success is NOT H07-qualified
        assert fake.loads==2
        report=json.loads((output/"owner-metrics.json").read_text())
        assert report[0]["cold_start_included"] is True
        assert report[0]["h07_status"] == "NOT_VERIFIED"
        assert report[0]["generation_state"] == "PASS"
        assert (output/"owner-metrics.csv").is_file()
        run_id=report[0]["run_id"]
        assert (output/"runs"/run_id/"longform"/"final.wav").is_file()
        assert (output/"runs"/run_id/"owner-metrics.csv").is_file()


@pytest.mark.parametrize("viewport", [(1280,900),(390,844)])
def test_rendered_custom_pause_caret_validation_and_generate(live, viewport):
    url, fake=live
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True)
        page=browser.new_page(viewport={"width":viewport[0],"height":viewport[1]})
        page.goto(url+"/ui")
        page.get_by_label("Voice name").fill("Owned")
        page.get_by_label("Exact transcript",exact=True).fill("An owned audio transcript.")
        page.locator('#clone-form input[name=ref_audio]').set_input_files({
            "name":"owned.wav","mimeType":"audio/wav","buffer":audio(6)})
        page.locator("#consent").check()
        page.get_by_role("button",name="Create voice").click()
        expect(page.get_by_text("Owned · version 1")).to_be_visible(timeout=10000)
        page.get_by_role("tab",name="Text to Speech").click()
        field=page.locator("#input")
        field.fill("Hello. Goodbye.")
        field.evaluate("(el)=>el.setSelectionRange(7,7)")
        custom=page.get_by_label("Custom 1–10000ms")
        custom.fill("800ms")
        page.get_by_role("button",name="+ custom pause").click()
        assert field.input_value()=="Hello. [pause 800ms]Goodbye."
        assert field.evaluate("(el)=>el.selectionStart")==len("Hello. [pause 800ms]")
        custom.fill("1.5s")
        field.evaluate("(el)=>el.setSelectionRange(el.value.length,el.value.length)")
        page.get_by_role("button",name="+ custom pause").click()
        assert field.input_value().endswith("[pause 1.5s]")
        frozen=field.input_value()
        for invalid in ("","0ms","10001ms","1.5555s","text"):
            custom.fill(invalid)
            page.get_by_role("button",name="+ custom pause").click()
            expect(page.locator("#notice")).to_contain_text("INVALID_PAUSE_MARKER")
            assert field.input_value()==frozen
        custom.fill("800ms")
        page.get_by_role("button",name="Generate WAV").click()
        expect(page.locator("#notice")).to_contain_text("Generation complete",timeout=15000)
        expect(page.locator("#metrics")).to_contain_text("pauses: 2")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        browser.close()
