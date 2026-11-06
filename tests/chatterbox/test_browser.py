"""Rendered Chromium behavioral tests: real DOM, API, and fake CUDA worker."""
from __future__ import annotations
import socket
import re
import json
import os
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




@pytest.mark.parametrize("viewport", [(1280,900), (390,844)])
def test_cloning_generate_library_and_runtime(live, viewport):
    url, fake = live
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": viewport[0], "height": viewport[1]})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url + "/ui")
        expect(page.locator(".sidebar")).to_be_visible()
        expect(page.get_by_role("heading", name="Voice Cloning").first).to_be_visible()
        page.locator("#clone-form input[name=name]").fill("Owned Voice")
        page.locator("#clone-form textarea[name=ref_text]").fill("This is the exact audio transcript")
        page.locator("#clone-file").set_input_files({
            "name": "owned.wav", "mimeType": "audio/wav", "buffer": audio(6)})
        expect(page.locator("#clone-meta")).to_contain_text("owned.wav")
        page.locator("#clone-consent").check()
        page.locator("#clone-submit").click()
        expect(page.locator("#voice-list")).to_contain_text("Owned Voice", timeout=12000)
        expect(page.locator("#voice-list")).to_contain_text("version 1")
        page.locator("#voice-list button").filter(has_text="Edit").click()
        page.locator("#reference-play").click()
        expect(page.locator("#saved-preview")).to_be_visible()
        page.locator('.nav-button[data-view=tts]').click()
        page.locator("#tts-text").fill("Hello. [pause 250ms] Goodbye.")
        page.locator("#tts-speed").evaluate(
            "(el) => { el.value = '1.15'; el.dispatchEvent(new Event('input',{bubbles:true})); }")
        page.locator("#tts-submit").click()
        expect(page.locator("#tts-state")).to_have_text("Completed", timeout=16000)
        expect(page.locator("#tts-download")).to_have_attribute("href", re.compile("^blob:"))
        expect(page.locator("#tts-breakdown")).to_contain_text("Speech WPM")
        expect(page.locator("#tts-breakdown")).to_contain_text("Speech spans")
        expect(page.locator("#tts-breakdown")).to_contain_text("Native chunks")
        page.locator('.nav-button[data-view=runtime]').click()
        expect(page.locator("#runtime-summary")).to_contain_text("Eligible to start")
        expect(page.locator("#runtime-summary")).to_contain_text("CUDA verified")
        # Synthetic CUDA does NOT confer true CUDA verification.
        expect(page.locator("#runtime-summary")).to_contain_text("Not verified")
        page.locator("#worker-idle").select_option("300")
        page.locator("#worker-save").click()
        expect(page.locator("#notice")).to_contain_text("Idle timeout saved")
        page.locator("#worker-unload").click()
        expect(page.locator("#notice")).to_contain_text("Nano worker unloaded")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert not errors, errors
        browser.close()


def test_error_and_cancel_controls(live):
    url, fake = live
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        observed = []
        page.on("request", lambda req: observed.append((req.url, req.headers, req.post_data_json))
                if req.method == "POST" and
                (req.url.endswith("/v1/audio/speech") or req.url.endswith("/lite/worker/cancel")) else None)
        page.goto(url + "/ui")
        page.locator('.nav-button[data-view=tts]').click()
        expect(page.locator("#tts-submit")).to_be_disabled()
        page.locator('.nav-button[data-view=clone]').click()
        page.locator("#clone-form input[name=name]").fill("Own")
        page.locator("#clone-form textarea[name=ref_text]").fill("Exact transcript")
        page.locator("#clone-file").set_input_files({
            "name": "owned.wav", "mimeType": "audio/wav", "buffer": audio(6)})
        page.locator("#clone-consent").check()
        page.locator("#clone-submit").click()
        expect(page.locator("#voice-list")).to_contain_text("Own", timeout=12000)
        fake.fail = True
        page.locator('.nav-button[data-view=tts]').click()
        page.locator("#tts-text").fill("Hello.")
        page.locator("#tts-submit").click()
        expect(page.locator("#notice")).to_contain_text("WORKER_FAILURE", timeout=16000)
        expect(page.locator("#tts-output")).to_be_hidden()
        fake.fail = False
        fake.delaying = True
        page.locator("#tts-submit").click()
        expect(page.locator("#tts-cancel")).to_be_enabled()
        page.locator("#tts-cancel").click()
        expect(page.locator("#notice")).to_contain_text(
            re.compile("cancellation|Cancellation|NO_ACTIVE_REQUEST|cancelled", re.I))
        speech = [item for item in observed if item[0].endswith("/v1/audio/speech")][-1]
        cancel = [item for item in observed if item[0].endswith("/lite/worker/cancel")][-1]
        assert speech[1]["x-tts-request-id"] == cancel[2]["request_id"]
        assert speech[1]["x-tts-cancel-token"] == cancel[2]["cancel_token"]
        expect(page.locator("#tts-output")).to_be_hidden()
        browser.close()


@pytest.mark.parametrize("viewport", [(1280,900), (390,844)])
def test_selected_file_preview_revocation_and_transcript_version(live, viewport):
    url, _ = live
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": viewport[0], "height": viewport[1]})
        page.add_init_script("""window.__revoked=[]; const old=URL.revokeObjectURL.bind(URL);
            URL.revokeObjectURL=(u)=>{window.__revoked.push(u);old(u)};""")
        page.goto(url + "/ui")
        page.locator("#clone-form input[name=name]").fill("Preview Voice")
        page.locator("#clone-form textarea[name=ref_text]").fill("Original exact transcript")
        for filename in ("first.wav", "second.wav"):
            page.locator("#clone-file").set_input_files({
                "name": filename, "mimeType": "audio/wav", "buffer": audio(6)})
            expect(page.locator("#clone-meta")).to_contain_text(filename)
        assert page.evaluate("window.__revoked.length") >= 1
        page.locator("#clone-consent").check()
        page.locator("#clone-submit").click()
        expect(page.locator("#voice-list")).to_contain_text("Preview Voice", timeout=12000)
        page.locator("#voice-list button").filter(has_text="Edit").click()
        expect(page.locator("#editor-id")).to_contain_text("version 1")
        page.on("dialog", lambda dialog: dialog.accept())
        page.locator("#edit-form textarea[name=ref_text]").fill("Corrected exact transcript")
        page.locator("#edit-save").click()
        expect(page.locator("#editor-id")).to_contain_text("version 2", timeout=12000)
        assert page.locator("#edit-form textarea[name=ref_text]").input_value() == "Corrected exact transcript"
        assert page.evaluate("window.__revoked.length") >= 2
        browser.close()


@pytest.mark.parametrize("viewport", [(1280,900), (390,844)])
def test_rendered_custom_pause_caret_validation_and_generate(live, viewport):
    url, _ = live
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": viewport[0], "height": viewport[1]})
        page.goto(url + "/ui")
        page.locator("#clone-form input[name=name]").fill("Owned")
        page.locator("#clone-form textarea[name=ref_text]").fill("An owned audio transcript.")
        page.locator("#clone-file").set_input_files({
            "name": "owned.wav", "mimeType": "audio/wav", "buffer": audio(6)})
        page.locator("#clone-consent").check()
        page.locator("#clone-submit").click()
        expect(page.locator("#voice-list")).to_contain_text("Owned", timeout=12000)
        page.locator('.nav-button[data-view=tts]').click()
        field = page.locator("#tts-text")
        field.fill("Hello. Goodbye.")
        field.evaluate("(el)=>el.setSelectionRange(7,14)")
        page.locator("#tts-pause-preset").select_option("custom")
        custom = page.locator("#tts-pause-custom")
        custom.fill("800")
        page.locator("#tts-insert-pause").click()
        assert field.input_value() == "Hello. [pause 800ms]Goodbye."
        assert field.evaluate("(el)=>el.selectionStart") == len("Hello. [pause 800ms]")
        assert field.evaluate("(el)=>el.selectionEnd") == len("Hello. [pause 800ms]Goodbye.")
        frozen = field.input_value()
        for invalid in ("", "0", "10001", "1.5"):
            custom.fill(invalid)
            page.locator("#tts-insert-pause").click()
            expect(page.locator("#notice")).to_contain_text("Pause must be")
            assert field.input_value() == frozen
        custom.fill("1500")
        field.evaluate("(el)=>el.setSelectionRange(el.value.length,el.value.length)")
        page.locator("#tts-insert-pause").click()
        assert field.input_value().endswith("[pause 1500ms]")
        page.locator("#tts-submit").click()
        expect(page.locator("#tts-state")).to_have_text("Completed", timeout=16000)
        expect(page.locator("#tts-breakdown")).to_contain_text("Speech spans")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        browser.close()


@pytest.mark.parametrize("viewport", [(1280,900), (390,844)])
def test_lite_baseline_visual_screenshots(live, tmp_path, viewport):
    """Capture original Lite and Nano at identical viewports; compare layout geometry and CSS."""
    from pathlib import Path
    import re as _re
    url, _ = live
    root = Path(__file__).resolve().parents[2]
    html = (root / "lite/ui/index.html").read_text(encoding="utf8")
    css = (root / "lite/ui/assets/app.css").read_text(encoding="utf8")
    html = _re.sub(r'<script[^>]*src="/ui/assets/app.js"[^>]*></script>', '', html)
    html = html.replace('<link rel="stylesheet" href="/ui/assets/app.css">', '<style>' + css + '</style>')
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        nano = browser.new_page(viewport={"width": viewport[0], "height": viewport[1]})
        baseline = browser.new_page(viewport={"width": viewport[0], "height": viewport[1]})
        nano.goto(url + "/ui")
        baseline.set_content(html)
        evidence = Path(os.environ.get("NANO_VISUAL_EVIDENCE_DIR", str(tmp_path / "visual"))) / (str(viewport[0]) + "x" + str(viewport[1]))
        evidence.mkdir(parents=True)
        for screen in ("clone", "library", "tts", "runtime"):
            nano.locator('.nav-button[data-view="' + screen + '"]').click()
            baseline.evaluate("""screen => {
              document.querySelectorAll('.view').forEach(el => el.hidden = el.id !== 'view-' + screen);
              document.querySelectorAll('.nav-button').forEach(el => el.classList.toggle('active', el.dataset.view === screen));
              document.querySelector('#page-title').textContent =
                ({clone:'Voice Cloning',library:'Voice Library',tts:'Text to Speech',runtime:'Runtime & Settings'})[screen];
            }""", screen)
            before = baseline.evaluate("""() => {
              const css = el => getComputedStyle(document.querySelector(el));
              const box = el => document.querySelector(el).getBoundingClientRect().toJSON();
              return {sidebar:box('.sidebar'),topbar:box('.topbar'),panel:box('#view-' +
                [...document.querySelectorAll('.view')].find(x=>!x.hidden).id.slice(5)+' .panel'),
                background:css('body').backgroundColor,nav:css('.nav-button').fontSize,
                radius:css('.panel').borderRadius};
            }""")
            after = nano.evaluate("""() => {
              const css = el => getComputedStyle(document.querySelector(el));
              const box = el => document.querySelector(el).getBoundingClientRect().toJSON();
              return {sidebar:box('.sidebar'),topbar:box('.topbar'),panel:box('#view-' +
                [...document.querySelectorAll('.view')].find(x=>!x.hidden).id.slice(5)+' .panel'),
                background:css('body').backgroundColor,nav:css('.nav-button').fontSize,
                radius:css('.panel').borderRadius};
            }""")
            assert before["background"] == after["background"]
            assert before["nav"] == after["nav"]
            assert before["radius"] == after["radius"]
            for section in ("sidebar", "topbar", "panel"):
                for key in ("x", "width"):
                    assert abs(before[section][key] - after[section][key]) <= 2, (screen, section, key, before, after)
            # Screenshots are emitted in test evidence; geometry checks do not substitute for owner visual review.
            nano.screenshot(path=str(evidence / (screen + "-nano.png")), full_page=True)
            baseline.screenshot(path=str(evidence / (screen + "-lite.png")), full_page=True)
        assert nano.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
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


