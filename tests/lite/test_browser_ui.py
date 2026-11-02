"""Rendered Chromium E2E: same FastAPI/API/FFmpeg path, synthetic fake-native only.

This is NOT real NVIDIA GTX1650 validation; that remains owner-host U05/U20.
"""
from __future__ import annotations
import asyncio
import hashlib
import io
import json
import socket
import sqlite3
import threading
import time
import wave
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import sync_playwright,expect

from lite.app import create_app
from lite.config import Settings,MODEL_REVISION,REPO_ID
from lite.licence import Licence
from lite.native_runner import NativeBusy,NativeFailure

def alternate_audio():
    changed=bytearray(audio());changed[-1]=0x07
    return bytes(changed)

def audio():
    out=io.BytesIO()
    with wave.open(out,"wb") as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000)
        w.writeframes(b"\x10\x02"*6000)
    return out.getvalue()

class FakeNative:
    def __init__(self):
        self.lock=asyncio.Lock()
        self.active_id=None
        self.count=0
        self.attempts=0
        self.fail_on=None
        self.slow_on=None
        self.observed=[]
    async def generate(self,*,profile,reference,text,language,seed,request_id,speed=1.0):
        self.attempts+=1
        if self.fail_on==self.attempts:
            raise NativeFailure("Synthetic requested failure")
        if text=="busy":
            raise NativeBusy("Synthetic busy")
        if text=="failed":
            raise NativeFailure("Synthetic failure")
        async with self.lock:
            self.observed.append({"id":profile["id"],"ref_text":profile["ref_text"],
                                  "reference":reference.read_bytes(),"version":profile["version"],"speed":speed})
            self.active_id=profile["id"]
            try:
                await asyncio.sleep(1.1 if text=="slow" or self.slow_on==self.attempts else .025)
                self.count+=1
                return audio(),{"request_id":request_id,"observed_native_backend":"Vulkan0",
                    "observed_device_identity":"NVIDIA GeForce GTX 1650",
                    "verification_status":"verified","elapsed_s":.025,"audio_s":.25,"rtf":.1}
            finally:
                self.active_id=None
    async def stop(self):
        return None

@pytest.fixture
def ui_server(tmp_path,monkeypatch):
    root=tmp_path/"legacy"
    root.mkdir()
    (root/"voices").mkdir()
    (root/"voices"/"legacy.wav").write_bytes(audio())
    with sqlite3.connect(root/"omnivoice.db") as db:
        db.execute("CREATE TABLE voice_profiles(id TEXT,name TEXT,ref_audio_path TEXT,ref_text TEXT,language TEXT,kind TEXT)")
        db.execute("INSERT INTO voice_profiles VALUES(?,?,?,?,?,?)",
                   ("legacy-ink","Legacy Ink","legacy.wav","Old exact words","en","clone"))
    monkeypatch.setenv("GGUF_LITE_LEGACY_ROOT",str(root))
    s=Settings(data_dir=tmp_path/"owned",cache_dir=tmp_path/"models",bin_dir=tmp_path/"bin",timeout=30)
    licence=Licence(s)
    provenance=s.data_dir/"provenance"
    provenance.mkdir()
    reviewed=b"synthetic test-only pinned provenance"
    (provenance/"README.md").write_bytes(reviewed)
    (provenance/"review.json").write_text(json.dumps({
        "model_revision":MODEL_REVISION,"repo_id":REPO_ID,
        "evidence_url":"https://huggingface.co/"+REPO_ID+"/blob/"+MODEL_REVISION+"/README.md",
        "model_licence":"CC-BY-NC-4.0","commercial_inference":"restricted",
        "reviewed_by":"CI fixture not human review","reviewed_at":"fixture",
        "document_sha256":hashlib.sha256(reviewed).hexdigest()}),encoding="utf8")
    import lite.app as module
    def synthetic_preflight(_):
        return {"ready":True,"quant":{"selected":"omnivoice-base-Q8_0.gguf","installed":True,
                "verified":True,"prepared_quant":"Q8_0","in_use":None,
                "base":"omnivoice-base-Q8_0.gguf","tokenizer":"omnivoice-tokenizer-Q8_0.gguf",
                "revision":MODEL_REVISION,"runtime_revision":"886fc079838ca7400cb2b42b36e2a65aa1daabe8"},
                "gpu":{"verified":True,"name":"NVIDIA GeForce GTX 1650","vendor_id":"0x10de","vram_mib":4096},
                "binary":True,"issues":[]}
    monkeypatch.setattr(module,"preflight",synthetic_preflight)
    native=FakeNative()
    app=create_app(settings=s,licence=licence,runner=native)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",port=port,
                                        log_level="error",lifespan="on",access_log=False))
    thread=threading.Thread(target=server.run,daemon=True)
    thread.start()
    for _ in range(150):
        if server.started:break
        time.sleep(.04)
    assert server.started,"Ephemeral local FastAPI server could not start"
    yield "http://127.0.0.1:"+str(port),native
    server.should_exit=True
    thread.join(timeout=5)
    assert not thread.is_alive()

@pytest.mark.parametrize("viewport",[{"width":1280,"height":900},{"width":390,"height":844}],ids=["desktop-1280","mobile-390"])
@pytest.mark.timeout(90)
def test_real_browser_clone_licence_tts_library_migration_and_recovery(ui_server,viewport):
    base,native=ui_server
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page(viewport=viewport,accept_downloads=True)
        page.on("dialog",lambda dialog:dialog.accept())
        page.goto(base+"/ui",wait_until="domcontentloaded")
        expect(page.locator("#clone-form")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 2"),"UI horizontally overflows viewport"
        page.keyboard.press("Tab")
        assert page.evaluate("['BUTTON','INPUT','SELECT','A'].includes(document.activeElement.tagName)")
        from pathlib import Path as _Path
        shots=_Path("ui-test-artifacts")
        shots.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(shots/("cloning-"+str(viewport["width"])+".png")),full_page=True)
        assert page.locator("script[src^='http']").count()==0
        page.locator("#clone-form input[name=name]").fill("New owner voice")
        page.locator("#clone-file").set_input_files({
          "name":"new-owned.wav","mimeType":"audio/wav","buffer":audio()})
        expect(page.locator("#clone-preview")).to_be_visible()
        page.locator("#clone-form textarea[name=ref_text]").fill("Exactly what was spoken")
        page.locator("#clone-consent").check()
        page.locator("#clone-submit").click()
        expect(page.locator("#view-tts")).to_be_visible()
        voice=page.locator("#tts-voice").input_value()
        assert voice and voice!="legacy-ink"
        page.locator("#tts-text").fill("A new owner controlled clone")
        page.locator("#tts-submit").click()
        expect(page.locator("#tts-state")).to_have_text("Failed")
        expect(page.locator("#notice")).to_contain_text("terms acceptance")
        assert native.count==0
        page.locator('[data-view="runtime"]').click()
        expect(page.locator("#runtime-summary")).to_contain_text("Not ready",timeout=10000)
        expect(page.locator("#licence-details")).to_contain_text("RESTRICTED / NOT CLEARED")
        expect(page.locator("#licence-notice")).to_contain_text("CC-BY-NC-4.0")
        expect(page.locator("#licence-details")).to_contain_text("MISMATCH")
        page.locator("#licence-review").check()
        page.locator("#licence-accept").click()
        # Busy-disabled is not proof the persisted acceptance POST completed.
        expect(page.locator("#licence-details")).to_contain_text("Accepted locally",timeout=15000)
        expect(page.locator("#licence-accept")).to_be_disabled()
        page.locator('[data-view="tts"]').click()
        page.locator("#tts-text").fill("A new owner controlled clone")
        page.locator("#tts-submit").click()
        expect(page.locator("#tts-output")).to_be_visible(timeout=15000)
        expect(page.locator("#tts-proof")).to_contain_text("NVIDIA GeForce GTX 1650")
        assert page.locator("#tts-player").get_attribute("src").startswith("blob:")
        with page.expect_download() as result:
            page.locator("#tts-download").click()
        assert result.value.suggested_filename.startswith("lite-")
        page.locator("#tts-text").fill("busy")
        page.locator("#tts-submit").click()
        expect(page.locator("#tts-state")).to_have_text("Failed")
        expect(page.locator("#notice")).to_contain_text("already processing")
        page.locator("#tts-text").fill("Recovery after synthetic 429")
        page.locator("#tts-submit").click()
        expect(page.locator("#tts-output")).to_be_visible()
        page.locator("#tts-text").fill("slow")
        page.locator("#tts-submit").click()
        # Ensure native owns the slot BEFORE aborting; otherwise the POST may
        # race with navigation and the later 429 is correct, not a recovery bug.
        page.wait_for_function("() => fetch('/lite/status').then(r=>r.json()).then(s=>s.busy)",timeout=8000)
        page.locator('[data-view="library"]').click()
        expect(page.locator("#view-library")).to_be_visible()
        expect(page.locator("#tts-output")).to_be_hidden()
        page.wait_for_function("() => fetch('/lite/status').then(r=>r.json()).then(s=>!s.busy)",timeout=8000)
        page.locator('[data-view="tts"]').click()
        expect(page.locator("#tts-submit")).to_be_enabled()
        page.locator("#tts-text").fill("Recovered after navigation cancellation")
        def is_recovery(response):
            return (response.url.endswith("/v1/audio/speech")
                and response.request.method=="POST"
                and response.request.post_data_json.get("input")=="Recovered after navigation cancellation")
        with page.expect_response(is_recovery,timeout=12000) as first:
            page.locator("#tts-submit").click()
        assert first.value.status in (200,429)
        if first.value.status==429:
            # 429 is confirmed rejection, safe to submit only on another user click.
            expect(page.locator("#notice")).to_contain_text("already processing")
            page.wait_for_timeout(3200)
            with page.expect_response(is_recovery,timeout=12000) as second:
                page.locator("#tts-submit").click()
            assert second.value.status==200
        expect(page.locator("#tts-output")).to_be_visible(timeout=10000)
        page.locator('[data-view="library"]').click()
        expect(page.locator("#voice-list")).to_contain_text("New owner voice")
        page.locator("#voice-list .voice-item").filter(has_text="New owner voice").get_by_role("button",name="Edit").click()
        expect(page.locator("#editor-panel")).to_be_visible()
        page.locator('#edit-form input[name="name"]').fill("Renamed voice")
        page.locator("#edit-save").click()
        expect(page.locator("#voice-list")).to_contain_text("Renamed voice")
        page.locator("#reference-play").click()
        expect(page.locator("#saved-preview")).to_be_visible()
        # No UI submission without new transcript or a deliberate same-words choice.
        original=page.request.get(base+"/profiles/"+voice).json()
        page.locator('#replace-consent').check()
        page.locator('#replace-form input[type=file]').set_input_files({
          "name":"replacement.wav","mimeType":"audio/wav","buffer":alternate_audio()})
        expect(page.locator("#replace-preview")).to_be_visible()
        page.locator("#replace-submit").click()
        expect(page.locator("#editor-id")).to_contain_text("version 1")
        page.locator("#replace-text").fill("Different spoken words on recording B")
        stable_id=voice
        page.locator("#replace-submit").click()
        expect(page.locator("#editor-id")).to_contain_text("version 2")
        expect(page.locator('#edit-form textarea[name="ref_text"]')).to_have_value("Different spoken words on recording B")
        after=page.request.get(base+"/profiles/"+stable_id).json()
        assert after["ref_text"]=="Different spoken words on recording B"
        assert after["version"]==2
        assert after["source_sha256"]!=original["source_sha256"]
        assert after["ref_sha256"]!=original["ref_sha256"]
        assert after["id"]==stable_id
        page.locator("#reference-play").click()
        expect(page.locator("#saved-preview")).to_be_visible()
        page.locator('[data-view="tts"]').click()
        page.locator("#tts-text").fill("Use the replacement recording")
        page.locator("#tts-submit").click()
        expect(page.locator("#tts-output")).to_be_visible()
        assert native.observed[-1]["id"]==stable_id
        assert native.observed[-1]["ref_text"]=="Different spoken words on recording B"
        assert native.observed[-1]["version"]==2
        assert native.observed[-1]["reference"][:4]==b"RIFF"
        page.locator('[data-view="library"]').click()
        expect(page.locator("#editor-panel")).to_be_visible()
        expect(page.locator("#editor-consent")).to_contain_text("acknowledged")
        assert not page.locator("#replace-consent").is_checked()
        assert not page.locator("#replace-same").is_checked()
        assert page.locator("#replace-text").input_value()==""
        page.locator("#migration-preview").click()
        expect(page.locator("#migration-list")).to_contain_text("legacy-ink")
        page.locator("#migration-choice").select_option("legacy-ink")
        page.locator("#migration-apply").click()
        expect(page.locator("#migration-feedback")).to_contain_text("Imported profile legacy-ink")
        expect(page.locator("#voice-list")).to_contain_text("Legacy Ink")
        page.locator("#delete-purge").click()
        expect(page.locator("#editor-panel")).to_be_hidden()
        expect(page.locator("#voice-list")).not_to_contain_text("Renamed voice")
        page.locator('[data-view="runtime"]').click()
        expect(page.locator("#runtime-summary")).to_contain_text("Q8_0")
        page.locator("#diagnostic-start").click()
        expect(page.locator("#diagnostic-state")).to_contain_text("3/3 verified",timeout=15000)
        assert page.locator("#diagnostic-rows tr").count()==3
        expect(page.locator("#diagnostic-rows")).to_contain_text("NVIDIA GeForce GTX 1650")
        expect(page.locator("#diagnostic-export")).to_be_enabled()
        with page.expect_download() as exported:
            page.locator("#diagnostic-export").click()
        report=json.loads(Path(exported.value.path()).read_text(encoding="utf-8"))
        assert report["status"]=="complete" and len(report["runs"])==3
        assert all(row["verified"] and row["client_rtf"]>0 for row in report["runs"])
        assert report["comparable"] is False and "input" not in report
        native.fail_on=native.attempts+2
        page.locator("#diagnostic-start").click()
        expect(page.locator("#diagnostic-state")).to_contain_text("failed after 1/3",timeout=15000)
        assert page.locator("#diagnostic-rows tr").count()==2
        assert page.locator("#diagnostic-export").is_disabled()
        native.fail_on=None
        page.locator("#diagnostic-start").click()
        expect(page.locator("#diagnostic-state")).to_contain_text("3/3 verified",timeout=15000)
        assert page.locator("#diagnostic-rows tr").count()==3
        native.slow_on=native.attempts+2
        page.locator("#diagnostic-start").click()
        expect(page.locator("#diagnostic-rows tr")).to_have_count(1,timeout=10000)
        page.wait_for_function("() => fetch('/lite/status').then(r=>r.json()).then(x=>x.busy)",timeout=5000)
        page.locator('[data-view="library"]').click()
        expect(page.locator("#diagnostic-export")).to_be_disabled()
        page.locator('[data-view="runtime"]').click()
        native.slow_on=None
        expect(page.locator("#diagnostic-export")).to_be_disabled()
        expect(page.locator("#diagnostic-state")).to_contain_text("cancelled")
        page.locator("#licence-withdraw").click()
        expect(page.locator("#licence-accept")).to_be_enabled()
        assert native.count>=5
        page.screenshot(path=str(shots/("runtime-"+str(viewport["width"])+".png")),full_page=True)
        browser.close()

@pytest.mark.timeout(60)
def test_runtime_ffmpeg_installed_and_missing_truth(ui_server,monkeypatch):
    base,_=ui_server
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page(viewport={"width":390,"height":844})
        import lite.app as appmodule
        monkeypatch.setattr(appmodule.shutil,"which",lambda name:"/fake/ffmpeg" if name=="ffmpeg" else None)
        page.goto(base+"/ui",wait_until="domcontentloaded")
        page.locator('[data-view="runtime"]').click()
        expect(page.locator("#runtime-details")).to_contain_text("Installed",timeout=10000)
        assert page.request.get(base+"/lite/status").json()["ffmpeg_installed"] is True
        monkeypatch.setattr(appmodule.shutil,"which",lambda _:None)
        page.locator("#runtime-refresh").click()
        expect(page.locator("#runtime-details")).to_contain_text("Missing — install FFmpeg",timeout=10000)
        assert page.request.get(base+"/lite/status").json()["ffmpeg_installed"] is False
        browser.close()


@pytest.mark.timeout(70)
def test_n03_one_native_preflight_per_manual_chromium_refresh(ui_server,monkeypatch):
    base,_=ui_server
    import lite.app as module
    original=module.preflight
    calls=[]
    def counted(settings):
        calls.append("preflight")
        return original(settings)
    monkeypatch.setattr(module,"preflight",counted)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page(viewport={"width":1280,"height":900})
        seen=[]
        page.on("request",lambda request:seen.append(request.url))
        page.goto(base+"/ui",wait_until="domcontentloaded")
        page.locator('[data-view="runtime"]').click()
        expect(page.locator("#runtime-refresh")).to_be_enabled(timeout=15000)
        calls.clear()
        seen.clear()
        page.locator("#runtime-refresh").click()
        expect(page.locator("#runtime-refresh")).to_be_enabled(timeout=15000)
        assert len(calls)==1, ("Each UI click must run precisely one preflight",calls)
        assert not any("/engines/omnivoice-gguf/quants" in u for u in seen),seen
        assert sum("/lite/readiness" in u for u in seen)==1,seen
        before=len(calls)
        response=page.request.get(base+"/engines/omnivoice-gguf/quants")
        assert response.status==200
        assert len(calls)==before+1,"Independent quants route must still verify engine"
        calls.clear()
        # Error must invalidate prior green state; next explicit click can recover.
        fail={"active":True}
        def intermittent(settings):
            calls.append("checked")
            if fail["active"]:
                raise RuntimeError("Synthetic failed preflight")
            return original(settings)
        monkeypatch.setattr(module,"preflight",intermittent)
        page.locator("#runtime-refresh").click()
        expect(page.locator("#runtime-checked")).to_contain_text("FAILED",timeout=15000)
        expect(page.locator("#runtime-summary")).to_contain_text("NOT CURRENT")
        assert page.locator("#runtime-details .detail").count()==0
        fail["active"]=False
        page.locator("#runtime-refresh").click()
        expect(page.locator("#runtime-checked")).to_contain_text("Last checked",timeout=15000)
        expect(page.locator("#runtime-summary")).to_contain_text("Q8_0")
        assert len(calls)==2
        prior=len(calls)
        # Two synchronous clicks while one request is pending must not trigger
        # two concurrent full model hashes; subsequent manual click still works.
        page.evaluate("() => { const b=document.getElementById('runtime-refresh');b.click();b.click(); }")
        expect(page.locator("#runtime-refresh")).to_be_enabled(timeout=15000)
        assert len(calls)==prior+1
        browser.close()

@pytest.mark.timeout(60)
def test_n01_rendered_same_words_requires_explicit_confirmation(ui_server):
    base,native=ui_server
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page(viewport={"width":390,"height":844})
        page.goto(base+"/ui")
        page.locator('#clone-form input[name="name"]').fill("Same words voice")
        page.locator("#clone-file").set_input_files({
            "name":"original.wav","mimeType":"audio/wav","buffer":audio()})
        page.locator('#clone-form textarea[name="ref_text"]').fill("Unchanged spoken words")
        page.locator("#clone-consent").check()
        page.locator("#clone-submit").click()
        expect(page.locator("#view-tts")).to_be_visible()
        id=page.locator("#tts-voice").input_value()
        page.locator('[data-view="library"]').click()
        page.locator("#voice-list .voice-item").filter(has_text="Same words voice").get_by_role("button",name="Edit").click()
        page.locator("#replace-file").set_input_files({
            "name":"different-take.wav","mimeType":"audio/wav","buffer":alternate_audio()})
        page.locator("#replace-same").check()
        expect(page.locator("#replace-text")).to_be_disabled()
        page.locator("#replace-consent").check()
        page.locator("#replace-submit").click()
        expect(page.locator("#editor-id")).to_contain_text("version 2")
        saved=page.request.get(base+"/profiles/"+id).json()
        assert saved["ref_text"]=="Unchanged spoken words"
        assert saved["consent"]=="owner-confirmed-local"
        assert not page.locator("#replace-same").is_checked()
        browser.close()


@pytest.mark.timeout(60)
@pytest.mark.parametrize("width",[1280,390])
def test_native_speed_browser_real_json_request_and_measured_display(ui_server,monkeypatch,width):
    base,native=ui_server
    import lite.app as appmodule
    monkeypatch.setattr(appmodule,"native_speed_capability",
                        lambda _:{"supported":True,"marker":"LiteNativeSpeed/v1","min":0.75,"max":1.25})
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page(viewport={"width":width,"height":860})
        seen=[]
        page.on("request",lambda req:seen.append(req.post_data_json)
                if req.url.endswith("/v1/audio/speech") and req.method=="POST" else None)
        page.goto(base+"/ui")
        page.locator('#clone-form input[name="name"]').fill("Native speed synthetic")
        page.locator("#clone-file").set_input_files({
            "name":"owner.wav","mimeType":"audio/wav","buffer":audio()})
        page.locator('#clone-form textarea[name="ref_text"]').fill("A short reference transcript")
        page.locator("#clone-consent").check()
        page.locator("#clone-submit").click()
        expect(page.locator("#view-tts")).to_be_visible()
        expect(page.locator("#tts-speed")).to_be_enabled(timeout=10000)
        assert float(page.locator("#tts-speed").input_value())==1.0
        page.locator("#tts-speed").evaluate("(el) => {el.value='1.20';el.dispatchEvent(new Event('input',{bubbles:true}));}")
        expect(page.locator("#tts-speed-value")).to_have_text("1.20×")
        page.locator("#tts-text").fill("Five words to measure speech")
        page.locator("#tts-submit").click()
        expect(page.locator("#tts-output")).to_be_visible(timeout=15000)
        assert seen and seen[-1]["speed"]==1.2
        assert native.observed[-1]["speed"]==1.2
        expect(page.locator("#tts-timing")).to_contain_text("Requested 1.20×")
        expect(page.locator("#tts-timing")).to_contain_text("Approx measured")
        expect(page.locator("#tts-timing")).to_contain_text("Server RTF")
        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth+2")
        page.locator("#tts-speed").evaluate("(el) => {el.value='0.85';el.dispatchEvent(new Event('input',{bubbles:true}));}")
        page.locator("#tts-text").fill("Slow requested delivery of this sentence")
        page.locator("#tts-submit").click()
        expect(page.locator("#tts-output")).to_be_visible(timeout=15000)
        assert seen[-1]["speed"]==0.85
        assert native.observed[-1]["speed"]==0.85
        browser.close()

@pytest.mark.timeout(60)
def test_old_binary_disables_native_speed_ui_but_preserves_baseline(ui_server,monkeypatch):
    base,native=ui_server
    import lite.app as appmodule
    monkeypatch.setattr(appmodule,"native_speed_capability",
                        lambda _:{"supported":False,"reason":"old native runtime","min":0.75,"max":1.25})
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page()
        page.goto(base+"/ui")
        page.locator('#clone-form input[name="name"]').fill("Old baseline")
        page.locator("#clone-file").set_input_files({
            "name":"owner.wav","mimeType":"audio/wav","buffer":audio()})
        page.locator('#clone-form textarea[name="ref_text"]').fill("A reference sentence")
        page.locator("#clone-consent").check()
        page.locator("#clone-submit").click()
        expect(page.locator("#view-tts")).to_be_visible()
        expect(page.locator("#tts-speed")).to_be_disabled()
        expect(page.locator("#tts-speed-notice")).to_contain_text("requires rebuilt")
        assert float(page.locator("#tts-speed").input_value())==1.0
        page.locator("#tts-text").fill("Old binary can still speak at baseline")
        page.locator("#tts-submit").click()
        expect(page.locator("#tts-output")).to_be_visible(timeout=15000)
        assert native.observed[-1]["speed"]==1.0
        browser.close()
