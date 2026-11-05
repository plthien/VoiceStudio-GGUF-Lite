import hashlib
import json

from lite import chatterbox_config as config


def test_complete_pinned_manifest_verifies_and_tamper_fails(tmp_path, monkeypatch):
    settings = config.ChatterboxSettings(root=tmp_path)
    settings.model_dir.mkdir(parents=True)
    hashes = {}
    for name in config.REQUIRED_FILES:
        payload = ("synthetic-nano-ci:" + name).encode()
        (settings.model_dir / name).write_bytes(payload)
        hashes[name] = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(config, "MODEL_HASHES", hashes)
    manifest = {"schema":"chatterbox-nano-model-manifest-v1",
        "model_id":"chatterbox-nano", "repo_id":"ResembleAI/chatterbox-nano",
        "loader_mode":"nano=True", "revision":config.MODEL_REVISION,
        "source_revision":config.SOURCE_REVISION, "sha256":hashes}
    (settings.model_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf8")
    assert config.verify_model_files(settings) == (True, [])
    (settings.model_dir / "conds.pt").write_bytes(b"untrusted pickle")
    assert config.verify_model_files(settings) == (False, ["MODEL_MANIFEST_INVALID"])
    (settings.model_dir / "conds.pt").unlink()
    (settings.model_dir / "t3_nano_v1.safetensors").write_bytes(b"tampered")
    assert config.verify_model_files(settings) == (False, ["MODEL_HASH_MISMATCH"])


def test_manifest_identity_and_missing_files_fail_closed(tmp_path):
    settings = config.ChatterboxSettings(root=tmp_path)
    settings.model_dir.mkdir(parents=True)
    assert config.verify_model_files(settings) == (False, ["MODEL_MANIFEST_UNAVAILABLE"])
    (settings.model_dir / "manifest.json").write_text(json.dumps({
        "schema":"chatterbox-nano-model-manifest-v1", "model_id":"chatterbox-turbo",
        "repo_id":config.MODEL_REPO, "loader_mode":"nano=True",
        "revision":config.MODEL_REVISION, "source_revision":config.SOURCE_REVISION,
        "sha256":{}}), encoding="utf8")
    assert config.verify_model_files(settings) == (False, ["MODEL_REVISION_MISMATCH"])
