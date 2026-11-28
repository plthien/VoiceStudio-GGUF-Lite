import hashlib
from pathlib import Path

from lite.chatterbox_config import MODEL_ID, MODEL_REPO, MODEL_REVISION
from lite.profiles import Store
from .test_contracts import api, profile, gen, audio


def test_canonical_lite_contract_and_openapi(api):
    client, service, _ = api
    paths = client.get("/openapi.json").json()["paths"]
    required = {"/lite/status", "/lite/readiness", "/lite/model-capability",
        "/lite/speed-capability", "/lite/pause-capability", "/lite/worker/status",
        "/lite/worker/settings", "/lite/worker/unload", "/lite/worker/cancel",
        "/lite/migration/preview", "/lite/migration/apply", "/lite/rights",
        "/v1/audio/speech", "/v1/audio/voices"}
    assert required <= set(paths)
    cap = client.get("/lite/model-capability").json()
    assert cap["model"] == MODEL_ID and cap["repo_id"] == MODEL_REPO
    assert cap["loader_mode"] == "nano=True" and cap["model_revision"] == MODEL_REVISION
    assert client.get("/lite/speed-capability").json()["mode"] == "postprocess_atempo"
    assert service.loads == 0


def test_wrong_engine_rejected_before_worker_and_proof_is_truthful(api):
    client, service, _ = api
    owned = profile(client)
    wrong = client.post("/v1/audio/speech", json={"model":"chatterbox-turbo",
        "voice":owned["id"], "input":"Hello.", "language":"en"})
    assert wrong.status_code == 422 and service.loads == 0
    result = gen(client, owned["id"])
    assert result.status_code == 200
    assert result.headers["x-tts-engine"] == MODEL_ID
    assert result.headers["x-tts-backend"] == "CUDA0"
    assert result.headers["x-tts-speed-mode"] == "postprocess_atempo"
    assert len(result.headers["x-tts-execution-proof"]) == 64
    assert "vulkan" not in str(result.headers).lower() and "q8_0" not in str(result.headers).lower()


def test_multi_sentence_native_accounting_and_pre_admission_rejection(api):
    client, service, _ = api
    owned = profile(client)
    text = ("A short sentence. " * 36).strip()
    result = gen(client, owned["id"], text)
    assert result.status_code == 200
    assert result.headers["x-lite-speech-segments"] == "1"
    assert int(result.headers["x-tts-native-chunks"]) > 1
    loads = service.loads
    rejected = gen(client, owned["id"], "Invalid [laugh] marker.")
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "UNSUPPORTED_PARALINGUISTIC_TAG"
    assert service.loads == loads


def test_explicit_legacy_preview_apply_preserves_source(api, tmp_path, monkeypatch):
    client, _, target_settings = api
    legacy_root = tmp_path / "legacy"
    legacy_settings = type(target_settings)(root=legacy_root)
    source_store = Store(legacy_settings)
    row = source_store.import_audio(name="Legacy owned", ref_text="Exact owned words.",
        language="en", source=audio(6), suffix=".wav", profile_id="legacy_voice",
        consent="confirmed", create_with_id=True)
    before_db = hashlib.sha256(source_store.db.read_bytes()).hexdigest()
    before_ref = hashlib.sha256(source_store.reference(row).read_bytes()).hexdigest()
    monkeypatch.setenv("CHATTERBOX_NANO_LEGACY_ROOT", str(legacy_root))
    shown = client.get("/lite/migration/preview")
    assert shown.status_code == 200 and shown.json()["eligible"][0]["id"] == "legacy_voice"
    imported = client.post("/lite/migration/apply", json={"voice_id":"legacy_voice"})
    assert imported.status_code == 200 and imported.json()["migrated"] == 1
    assert client.get("/profiles/legacy_voice").status_code == 200
    assert hashlib.sha256(source_store.db.read_bytes()).hexdigest() == before_db
    assert hashlib.sha256(source_store.reference(row).read_bytes()).hexdigest() == before_ref
