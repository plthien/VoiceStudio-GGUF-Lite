"""Opt-in REAL model acceptance. Run only on Windows local with pinned 420 MB weights.
Never mark skipped checks as passing hardware gates.
Set VIENEU_MODEL_ACCEPTANCE=1 and run the local server before pytest.
"""
from __future__ import annotations
import base64
import hashlib
import json
import os
from pathlib import Path

import httpx
import pytest

from lite.audio import wav_info
from lite.vieneu_config import CONTRACT, MODEL_REVISION, PROVIDER

pytestmark = pytest.mark.skipif(os.environ.get("VIENEU_MODEL_ACCEPTANCE") != "1",
    reason="BLOCKED: requires installed Windows CPU/ONNX runtime and pinned model")

HOST = os.environ.get("VIENEU_ACCEPTANCE_URL", "http://127.0.0.1:3901").rstrip("/")
VIETNAMESE_CASES = [
    "Xin chào các bạn, hôm nay chúng ta gặp nhau ở Hà Nội.",
    "Ngày hai mươi tháng mười một là Ngày Nhà giáo Việt Nam.",
    "Cô giáo Hoa đang giảng bài cho các em học sinh.",
    "Bạn có biết vì sao bầu trời có màu xanh không?",
    "Năm hai nghìn không trăm hai mươi sáu, chúng ta học được điều gì?",
    "Từ Đà Nẵng đến Thành phố Hồ Chí Minh có rất nhiều câu chuyện.",
    "Âm thanh nhẹ nhàng, tình cảm và giàu biểu cảm.",
    "Ba trăm năm mươi, tám trăm và một nghìn năm trăm mili giây.",
    "Một ngày mưa, cậu bé bỗng nhìn thấy ánh sáng kỳ lạ.",
    "Cảm ơn mọi người đã lắng nghe. Hẹn gặp lại!"
]
BASE_SENTENCE = "Hôm nay chúng ta kể một câu chuyện nhẹ nhàng về gia đình, thầy cô và những ngày tháng đáng nhớ. "

def _client():
    return httpx.Client(base_url=HOST, timeout=3600)

def _speak(client, text, voice, **params):
    response = client.post("/vieneu/v1/audio/speech", json={
        "model": PROVIDER, "language": "vi", "voice": voice,
        "input": text, "response_format": "wav", "speed": 1.0, "seed": 42, **params})
    assert response.status_code == 200, response.text[:500]
    assert response.headers["x-tts-backend"] == "CPUExecutionProvider"
    assert response.headers["x-vieneu-contract"] == CONTRACT
    receipt = json.loads(base64.urlsafe_b64decode(response.headers["x-vieneu-receipt"] + "==="))
    assert receipt["model_revision"] == MODEL_REVISION
    assert hashlib.sha256(response.content).hexdigest() == receipt["output_sha256"]
    info = wav_info(response.content, max_seconds=600, max_bytes=32 * 1024 * 1024)
    assert info["frames"] == receipt["audio_frames"]
    return info, receipt, response

def _ready_and_preset(client):
    preflight = client.get("/vieneu/readiness").json()
    assert preflight["ready"] is True, preflight
    presets = client.get("/vieneu/presets")
    assert presets.status_code == 200, presets.text[:300]
    voices = presets.json()
    assert voices and all(v["id"].startswith("preset:") for v in voices)
    return voices[0]["id"]

def test_local_preset_ten_vietnamese_utterances():
    with _client() as client:
        voice = _ready_and_preset(client)
        for text in VIETNAMESE_CASES:
            _speak(client, text, voice)

@pytest.mark.parametrize("minutes,syllables", [(3, 480), (8, 1280)])
def test_local_real_longform_and_pause(minutes, syllables):
    text = " ".join((BASE_SENTENCE * (syllables // len(BASE_SENTENCE.split()) + 1)).split()[:syllables])
    with _client() as client:
        voice = _ready_and_preset(client)
        info, receipt, _ = _speak(client, text, voice)
        assert receipt["segments"] >= 2
        # Range is deliberately broad; pronunciation and boundary quality need listening.
        assert 60 * (minutes - 1) <= info["seconds"] <= min(600, 60 * (minutes + 2))
        info2, receipt2, _ = _speak(client, "Xin chào [pause 350ms] Việt Nam [pause 800ms] Hẹn gặp lại [pause 1.5s] nhé.", voice)
        assert receipt2["pause_frames"] == (350 + 800 + 1500) * 24

def test_local_clone_persistence_and_versioned_reference():
    file = os.environ.get("VIENEU_ACCEPTANCE_REF")
    if not file or not Path(file).is_file():
        pytest.skip("BLOCKED: VIENEU_ACCEPTANCE_REF must be a permitted real 5–8 second WAV/MP3")
    with _client() as client, open(file, "rb") as audio:
        _ready_and_preset(client)
        create = client.post("/vieneu/profiles", files={"ref_audio": (Path(file).name, audio)},
                             data={"name": "Local acceptance", "consent": "confirmed"})
        assert create.status_code == 200, create.text[:500]
        id_ = create.json()["voice_id"]
        try:
            assert client.get("/vieneu/profiles/" + id_ + "/integrity").json()["verified"] is True
            _speak(client, "Xin chào. Đây là giọng tiếng Việt được ghi nhớ.", id_)
            assert client.post("/vieneu/worker/unload").status_code == 200
            # Force cold load; stored conditioning is reused after unload (restart still manual).
            _speak(client, "Giọng của tôi vẫn được lưu trong thư viện.", id_)
            assert client.get("/vieneu/profiles/" + id_).json()["voice_version"] == 1
        finally:
            client.delete("/vieneu/profiles/" + id_)

def test_local_browser_provider_flow():
    if os.environ.get("VIENEU_BROWSER_E2E") != "1":
        pytest.skip("BLOCKED: opt-in local browser/Playwright acceptance")
    from playwright.sync_api import sync_playwright, expect
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(HOST + "/ui")
            page.locator("#provider-select").select_option("vieneu")
            expect(page.locator("#vi-tts-panel")).not_to_be_hidden()
            expect(page.locator("#vi-clone-panel")).not_to_be_hidden()
            page.locator('.nav-button[data-view="tts"]').click()
            expect(page.locator("#vi-tts-form")).to_be_visible()
            expect(page.locator("#tts-form")).not_to_be_visible()
            page.locator("#vi-tts-text").fill("Xin chào. Đây là giọng đọc tiếng Việt.")
            page.locator("#provider-select").select_option("chatterbox")
            page.locator("#provider-select").select_option("vieneu")
            assert page.locator("#vi-tts-text").input_value().startswith("Xin chào")
        finally:
            browser.close()
