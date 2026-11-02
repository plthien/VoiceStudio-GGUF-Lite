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
    async def generate(self,*,profile,reference,text,language,seed,request_id):
        if text=="busy":
            raise NativeBusy("Synthetic busy")
        if text=="failed":
            raise NativeFailure("Synthetic failure")
        async with self.lock:
            self.active_id=profile["id"]
            try:
                await asyncio.sleep(1.1 if text=="slow" else .025)
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
    with sqlite3.connect(root/"voices.sqlite3") as db:
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
        page.locator("#licence-review").check()
        page.locator("#licence-accept").click()
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
        page.locator("#tts-submit").click()
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
        page.locator('#replace-form input[type=file]').set_input_files({
          "name":"replacement.wav","mimeType":"audio/wav","buffer":audio()})
        page.locator("#replace-submit").click()
        expect(page.locator("#editor-id")).to_contain_text("version 2")
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
        assert page.locator("#diagnostic-export").is_enabled()
        with page.expect_download() as exported:
            page.locator("#diagnostic-export").click()
        assert exported.value.suggested_filename=="lite-ui-diagnostic.json"
        page.locator("#licence-withdraw").click()
        expect(page.locator("#licence-accept")).to_be_enabled()
        assert native.count>=5
        page.screenshot(path=str(shots/("runtime-"+str(viewport["width"])+".png")),full_page=True)
        browser.close()
