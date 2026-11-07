"""Producer-real FastAPI contract tests; CUDA is the deterministic resident fake."""
import base64
import hashlib
import json
import threading
import time
import io
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from lite.audio import wav_info
from lite.chatterbox_contract import (
    CONTRACT_VERSION, RECEIPT_FIELDS, build_receipt, canonical_json_bytes,
    decode_receipt, verify_http_response, weights_manifest_sha256,
)
from lite.chatterbox_config import MODEL_HASHES, MODEL_REVISION
from lite.chatterbox_assembly import reference_binding
from .test_contracts import api, audio, gen, profile

ROOT = Path(__file__).resolve().parents[2]


def _headers(request_id="owned-request-0001", token=None):
    token = token or base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").rstrip(b"=").decode()
    return {"X-TTS-Request-Id": request_id, "X-TTS-Cancel-Token": token}


def _speech(client, voice, text, *, headers=None, speed=1.0, seed=42):
    return client.post("/v1/audio/speech", headers=headers or {}, json={
        "model":"chatterbox-nano", "voice":voice, "input":text,
        "language":"en", "response_format":"wav", "speed":speed, "seed":seed})


def test_capability_v1_is_truthful_and_manifest_digest_tamper_sensitive(api):
    client, service, _ = api
    capability = client.get("/lite/model-capability").json()
    assert capability["contract_version"] == CONTRACT_VERSION
    assert capability["repo_id"] == "ResembleAI/chatterbox-nano"
    assert capability["weights_sha256"] == MODEL_HASHES
    assert capability["weights_manifest_sha256"] == weights_manifest_sha256()
    assert capability["loader_mode"] == "nano=True"
    assert capability["segmentation_policy"] == "nano_sentence_v2"
    assert capability["native_segment_target_chars"] == 520
    assert capability["native_segment_hard_chars"] == 600
    assert capability["api_max_input_characters"] == 4096
    assert capability["definite_429_nonadmission"] is True
    changed = dict(MODEL_HASHES)
    changed[sorted(changed)[0]] = "0" * 64
    assert weights_manifest_sha256(changed) != capability["weights_manifest_sha256"]
    assert service.loads == 0


def test_checked_in_golden_receipt_recomputes_byte_for_byte():
    fixture = json.loads((ROOT / "tests/fixtures/nano_api_v1/golden_receipt.json").read_text("utf8"))
    wav = base64.b64decode(fixture["wav_base64"], validate=True)
    text = fixture["input"]
    info = {"audio_frames":1, "speech_frames":1, "pause_frames":0,
            "pause_count":0, "speech_segments":1, "native_speech_segments":1,
            "chunk_text_sha256":[hashlib.sha256(text.encode()).hexdigest()],
            "model":"chatterbox-nano", "backend":"CUDA0", "model_revision":MODEL_REVISION}
    _obj, raw, token, proof = build_receipt(
        profile=fixture["profile"], reference_binding=fixture["reference_binding"],
        input_text=text, seed=fixture["seed"], speed=fixture["numeric_request_speed"],
        wav=wav, info=info)
    assert raw.decode() == fixture["receipt_json"]
    assert token == "".join(fixture["receipt_base64url_chunks"])
    assert proof == fixture["execution_proof_sha256"]
    decode_receipt(token, proof=proof, wav=wav)


def test_receipt_golden_shape_spans_chunks_and_exact_frames(api):
    client, _, _ = api
    voice = profile(client)
    sentence = "A" * 119 + ". "
    text = (sentence * 3).rstrip()
    response = _speech(client, voice["id"], text, speed=1.15, seed=7)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("audio/wav")
    assert response.headers["x-lite-speech-segments"] == "1"
    assert response.headers["x-tts-native-chunks"] == "1"
    assert response.headers["x-lite-pause-count"] == "0"
    assert response.headers["x-lite-pause-s"] == "0.000000000"
    token = response.headers["x-tts-execution-receipt"]
    proof = response.headers["x-tts-execution-proof"]
    receipt = decode_receipt(token, proof=proof, wav=response.content, expected={
        "voice_id":voice["id"], "voice_version":1,
        "input_sha256":hashlib.sha256(text.encode()).hexdigest(),
        "requested_speed":"1.150000", "seed":7,
    })
    assert set(receipt) == RECEIPT_FIELDS
    assert receipt["speech_span_count"] == 1 and receipt["native_chunk_count"] == 1
    assert receipt["audio_frames"] == receipt["speech_frames"]
    assert receipt["authored_pause_frames"] == 0
    assert len(token.encode("ascii")) <= 8192 and "=" not in token
    assert int(float(response.headers["x-lite-audio-s"]) * 24000) == receipt["audio_frames"]


def test_multi_span_pause_frame_accounting_and_zero_samples(api):
    client, _, _ = api
    voice = profile(client)
    response = _speech(client, voice["id"], "First. [pause 1ms] Second. [pause 800ms] Third. [pause 1.5s] Last.")
    assert response.status_code == 200
    receipt = decode_receipt(response.headers["x-tts-execution-receipt"],
                             proof=response.headers["x-tts-execution-proof"], wav=response.content)
    assert response.headers["x-lite-speech-segments"] == "4"
    assert response.headers["x-tts-native-chunks"] == "4"
    assert receipt["authored_pause_frames"] == (1 + 800 + 1500) * 24
    assert receipt["audio_frames"] == receipt["speech_frames"] + receipt["authored_pause_frames"]
    assert response.headers["x-lite-pause-count"] == "3"


@pytest.mark.parametrize(("marker","frames"), [
    ("1ms",24), ("800ms",19200), ("1.5s",36000), ("10s",240000),
])
def test_edge_authored_pause_is_exact_zero_pcm(api, marker, frames):
    client, _, _ = api
    voice = profile(client)
    response = _speech(client, voice["id"], f"[pause {marker}] Spoken.")
    assert response.status_code == 200
    receipt = decode_receipt(response.headers["x-tts-execution-receipt"],
                             proof=response.headers["x-tts-execution-proof"], wav=response.content)
    assert receipt["authored_pause_frames"] == frames
    with wave.open(io.BytesIO(response.content), "rb") as reader:
        assert reader.readframes(frames) == b"\0\0" * frames


@pytest.mark.parametrize("field", [
    "model", "backend", "model_revision", "source_revision", "loader_mode",
    "weights_manifest_sha256", "voice_id", "voice_version", "reference_sha256",
    "reference_binding", "input_sha256", "seed", "requested_speed",
    "segmentation_policy", "native_chunk_count", "native_chunk_sha256",
    "authored_pause_frames", "pause_count", "audio_frames",
])
def test_receipt_tampering_rejected(api, field):
    client, _, _ = api
    voice = profile(client)
    response = gen(client, voice["id"])
    token = response.headers["x-tts-execution-receipt"]
    proof = response.headers["x-tts-execution-proof"]
    receipt = decode_receipt(token, proof=proof, wav=response.content)
    if field == "native_chunk_sha256": receipt[field] = ["0" * 64]
    elif isinstance(receipt[field], bool): receipt[field] = not receipt[field]
    elif isinstance(receipt[field], int): receipt[field] += 1
    else: receipt[field] = "0" * 64
    altered = base64.urlsafe_b64encode(canonical_json_bytes(receipt)).rstrip(b"=").decode()
    with pytest.raises(ValueError):
        decode_receipt(altered, proof=proof, wav=response.content)


def test_wav_byte_tamper_and_context_mismatch_rejected(api):
    client, _, _ = api
    voice = profile(client)
    response = gen(client, voice["id"])
    token, proof = response.headers["x-tts-execution-receipt"], response.headers["x-tts-execution-proof"]
    changed = bytearray(response.content); changed[-1] ^= 1
    with pytest.raises(ValueError, match="WAV_MISMATCH"):
        decode_receipt(token, proof=proof, wav=bytes(changed))
    with pytest.raises(ValueError, match="CONTEXT_MISMATCH"):
        decode_receipt(token, proof=proof, expected={"voice_id":"someone-else"})


@pytest.mark.parametrize(("header","value"), [
    ("content-type", "application/octet-stream"),
    ("x-tts-audio-s", "9.000000000"),
    ("x-lite-speech-s", "0.000000000"),
    ("x-lite-speech-segments", "99"),
    ("x-tts-native-chunks", "99"),
])
def test_response_envelope_tampering_fails_before_publication(api, header, value):
    client, _, _ = api
    voice = profile(client)
    response = gen(client, voice["id"])
    headers = dict(response.headers)
    verify_http_response(headers, response.content)
    headers[header] = value
    with pytest.raises(ValueError, match="(CONTENT_TYPE|HEADER)_MISMATCH"):
        verify_http_response(headers, response.content)


def test_reference_attestation_and_transcript_versioning(api):
    client, service, _ = api
    voice = profile(client)
    first = client.post(f"/profiles/{voice['id']}/integrity/verify").json()
    assert first == {"verified":True, "binding":first["binding"], "voice_id":voice["id"],
                     "voice_version":1, "reference_sha256":voice["ref_sha256"]}
    assert gen(client, voice["id"]).status_code == 200
    named = client.put(f"/profiles/{voice['id']}", json={"name":"Renamed"}).json()
    assert named["version"] == 1
    same = client.post(f"/profiles/{voice['id']}/integrity/verify").json()
    assert same["binding"] == first["binding"]
    denied = client.put(f"/profiles/{voice['id']}", json={"ref_text":"Corrected transcript"})
    assert denied.status_code == 422
    changed = client.put(f"/profiles/{voice['id']}", json={
        "ref_text":"Corrected transcript", "confirm_transcript_change":True})
    assert changed.status_code == 200 and changed.json()["version"] == 2
    second = client.post(f"/profiles/{voice['id']}/integrity/verify").json()
    assert second["binding"] != first["binding"] and second["voice_version"] == 2
    assert not service.cache


def test_atomic_429_and_invalid_422_never_enter_worker(api):
    client, service, _ = api
    voice = profile(client)["id"]
    object.__setattr__(service.settings, "timeout", 5.0)
    service.delaying = True
    with ThreadPoolExecutor(max_workers=1) as pool:
        active = pool.submit(_speech, client, voice, "The admitted request.")
        for _ in range(100):
            if client.app.state.admission.locked(): break
            time.sleep(.01)
        rejected = _speech(client, voice, "The rejected request.")
        assert rejected.status_code == 429 and rejected.headers["retry-after"] == "1"
        assert active.result(timeout=10).status_code == 200
    assert service.syntheses == 1
    before = service.syntheses
    invalid = _speech(client, voice, "This [laugh] tag is unsupported.")
    assert invalid.status_code == 422 and service.syntheses == before


def test_request_owned_cancel_cross_client_and_aba_safe(api):
    client, service, _ = api
    voice = profile(client)["id"]
    service.delaying = True
    owned = _headers("owned-request-0001")
    with ThreadPoolExecutor(max_workers=1) as pool:
        active = pool.submit(_speech, client, voice, "Cancel this request.", headers=owned)
        for _ in range(100):
            if client.app.state.active_generation: break
            time.sleep(.01)
        assert client.post("/lite/worker/cancel", json={
            "request_id":"wrong-request-0001", "cancel_token":owned["X-TTS-Cancel-Token"]}).status_code == 403
        assert client.post("/lite/worker/cancel", json={
            "request_id":owned["X-TTS-Request-Id"],
            "cancel_token":base64.urlsafe_b64encode(b"different-secret-000000").rstrip(b"=").decode()}).status_code == 403
        accepted = client.post("/lite/worker/cancel", json={
            "request_id":owned["X-TTS-Request-Id"], "cancel_token":owned["X-TTS-Cancel-Token"]})
        assert accepted.status_code == 200
        assert active.result(timeout=10).status_code == 499
    assert client.post("/lite/worker/cancel", json={
        "request_id":owned["X-TTS-Request-Id"], "cancel_token":owned["X-TTS-Cancel-Token"]}).status_code == 404


def test_non_tokened_generation_cannot_be_cancelled_by_caller(api):
    client, service, _ = api
    voice = profile(client)["id"]
    object.__setattr__(service.settings, "timeout", 5.0); service.delaying=True
    with ThreadPoolExecutor(max_workers=1) as pool:
        active=pool.submit(_speech,client,voice,"Non cancellable API request.")
        for _ in range(100):
            if client.app.state.active_generation: break
            time.sleep(.01)
        denied=client.post("/lite/worker/cancel",json={
            "request_id":"unrelated-request", "cancel_token":_headers()["X-TTS-Cancel-Token"]})
        assert denied.status_code==409
        assert active.result(timeout=10).status_code==200


def test_readiness_is_cheap_and_does_not_fabricate_cuda(api, monkeypatch):
    client, service, _ = api
    calls = service.loads
    for _ in range(5):
        value = client.get("/lite/readiness").json()
        assert value["cold_load_eligible"] is False
        assert value["model_hash_verified"] is False
        assert value["cuda_verified"] is False
        assert value["live_generation_verified"] is False
        assert value["readiness_semantics"] == "eligible_to_attempt_cold_load_not_cuda_attestation"
    assert service.loads == calls == 0
    voice = profile(client)["id"]
    assert gen(client, voice).status_code == 200
    after = client.get("/lite/readiness").json()
    assert after["cuda_verified"] is False
    assert after["live_generation_verified"] is True
    assert after["worker"]["cuda"]["evidence"] == "synthetic_ci"
