"""End-to-end publication and resume receipt enforcement regressions."""
from __future__ import annotations

from email.message import Message
import base64
import hashlib
import json
from pathlib import Path

import pytest

from lite.chatterbox_assembly import do_job, reference_binding
from lite.chatterbox_benchmark import run_case
from lite.chatterbox_contract import canonical_json_bytes
from .receipt_fixture import nano_response_headers
from .test_contracts import audio


PROFILE = {
    "id": "owner-voice", "version": 1,
    "ref_sha256": "a" * 64, "source_sha256": "b" * 64,
}


def _profile_http(_base, path, *args, **kwargs):
    if path.endswith("/integrity/verify"):
        return {
            "verified": True,
            "voice_id": PROFILE["id"],
            "voice_version": PROFILE["version"],
            "reference_sha256": PROFILE["ref_sha256"],
            "binding": reference_binding(PROFILE, PROFILE["id"]),
        }
    return dict(PROFILE)


def _response(body, mutate=None):
    wav = audio(.25)
    headers = nano_response_headers(
        profile=PROFILE,
        binding=reference_binding(PROFILE, PROFILE["id"]),
        text=body["input"], wav=wav, seed=body.get("seed"),
        speed=body.get("speed", 1.0),
    )
    if mutate:
        headers = mutate(dict(headers))

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, size):
            return wav[:size]

    result = Response()
    result.headers = headers
    return result


def _wire_assembly(monkeypatch, mutate=None):
    import lite.chatterbox_assembly as assembly
    posts = []

    def post(request, **_kwargs):
        body = json.loads(request.data)
        posts.append(body)
        return _response(body, mutate)

    monkeypatch.setattr(assembly, "http_json", _profile_http)
    monkeypatch.setattr(assembly.urllib.request, "urlopen", post)
    return posts


def _manifest(tmp_path: Path) -> Path:
    path = tmp_path / "input.json"
    path.write_text(json.dumps({
        "voice": PROFILE["id"],
        "sections": [{"id": "one", "input": "Exact frozen section."}],
    }), encoding="utf-8")
    return path


def _delete(name):
    return lambda headers: {k: v for k, v in headers.items() if k != name}


def _replace(name, value):
    def mutate(headers):
        headers[name] = value
        return headers
    return mutate


def _alter_receipt(field, value):
    """Keep proof/header checksum internally consistent while changing context."""
    def mutate(headers):
        token = headers["X-TTS-Execution-Receipt"]
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        receipt = json.loads(raw)
        receipt[field] = value
        if field == "voice_version":
            headers["X-TTS-Voice-Version"] = str(value)
        elif field == "model":
            headers["X-TTS-Engine"] = str(value)
        elif field == "segmentation_policy":
            headers["X-TTS-Segmentation-Policy"] = str(value)
        changed = canonical_json_bytes(receipt)
        headers["X-TTS-Execution-Receipt"] = base64.urlsafe_b64encode(
            changed).rstrip(b"=").decode("ascii")
        headers["X-TTS-Execution-Proof"] = hashlib.sha256(changed).hexdigest()
        return headers
    return mutate


def _alter_speed(headers):
    token = headers["X-TTS-Execution-Receipt"]
    raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    receipt = json.loads(raw)
    receipt.update(requested_speed="1.150000", effective_speed="1.150000",
                   atempo_applied=True)
    changed = canonical_json_bytes(receipt)
    headers["X-TTS-Execution-Receipt"] = base64.urlsafe_b64encode(
        changed).rstrip(b"=").decode("ascii")
    headers["X-TTS-Execution-Proof"] = hashlib.sha256(changed).hexdigest()
    headers["X-TTS-Requested-Speed"] = "1.150000"
    headers["X-TTS-Effective-Speed"] = "1.150000"
    return headers


@pytest.mark.parametrize("mutate", [
    _delete("X-TTS-Backend"),
    _replace("X-TTS-Backend", "MOCK"),
    _replace("X-TTS-Backend", "CPU"),
    _replace("X-TTS-Backend", "Vulkan0"),
    _delete("X-TTS-Execution-Receipt"),
    _delete("X-TTS-Execution-Proof"),
    _replace("X-TTS-Execution-Receipt", "malformed"),
    _replace("X-TTS-Execution-Proof", "0" * 64),
    _replace("Content-Type", "application/octet-stream"),
    _replace("X-Lite-Speech-Segments", "2"),
    _replace("X-TTS-Native-Chunks", "2"),
    _replace("X-Test-Oversized", "x" * 33000),
])
def test_longform_rejects_unverified_http_without_publication(tmp_path, monkeypatch, mutate):
    posts = _wire_assembly(monkeypatch, mutate)
    output = tmp_path / "out"
    with pytest.raises(ValueError):
        do_job(_manifest(tmp_path), output)
    assert len(posts) == 1
    assert not (output / "section-one.wav").exists()
    assert not (output / "final.wav").exists()
    state = json.loads((output / "resume-manifest.json").read_text("utf-8"))
    assert state["sections"]["one"]["state"] == "UNCERTAIN"


def test_longform_rejects_duplicate_authoritative_header(tmp_path, monkeypatch):
    def duplicate(headers):
        message = Message()
        for key, value in headers.items():
            message[key] = value
        message["X-TTS-Backend"] = "CUDA0"
        return message

    _wire_assembly(monkeypatch, duplicate)
    output = tmp_path / "out"
    with pytest.raises(ValueError, match="EXECUTION_HEADER_AMBIGUOUS"):
        do_job(_manifest(tmp_path), output)
    assert not (output / "section-one.wav").exists()


@pytest.mark.parametrize("mutate", [
    _alter_receipt("voice_id", "other-voice"),
    _alter_receipt("voice_version", 2),
    _alter_receipt("reference_binding", "0" * 64),
    _alter_receipt("input_sha256", "0" * 64),
    _alter_receipt("seed", 43),
    _alter_speed,
    _alter_receipt("model", "other-model"),
    _alter_receipt("segmentation_policy", "other-policy"),
])
def test_longform_rejects_internally_consistent_wrong_context(tmp_path, monkeypatch, mutate):
    _wire_assembly(monkeypatch, mutate)
    output = tmp_path / "out"
    with pytest.raises(ValueError):
        do_job(_manifest(tmp_path), output)
    assert not (output / "section-one.wav").exists()


def test_valid_section_persists_original_envelope_and_resume_never_posts(tmp_path, monkeypatch):
    posts = _wire_assembly(monkeypatch)
    output = tmp_path / "out"
    result = do_job(_manifest(tmp_path), output)
    assert result["state"] == "PASS" and result["verified_sections"] == 1
    state = json.loads((output / "resume-manifest.json").read_text("utf-8"))
    record = state["sections"]["one"]
    assert state["schema"] == "chatterbox-job-v3"
    assert record["execution_receipt_verified"] is True
    assert record["execution_envelope"]["schema"] == "nano_http_response_envelope_v1"
    assert len(record["execution_envelope"]["headers"]["X-TTS-Execution-Receipt"]) > 100
    before = (output / "final.wav").read_bytes()
    assert do_job(_manifest(tmp_path), output, resume=True)["state"] == "PASS"
    assert len(posts) == 1 and (output / "final.wav").read_bytes() == before


def test_legacy_proofless_resume_requires_explicit_retry(tmp_path, monkeypatch):
    posts = _wire_assembly(monkeypatch)
    manifest = _manifest(tmp_path)
    output = tmp_path / "out"
    do_job(manifest, output)
    state_path = output / "resume-manifest.json"
    state = json.loads(state_path.read_text("utf-8"))
    state["schema"] = "chatterbox-job-v2"
    record = state["sections"]["one"]
    record.pop("execution_envelope")
    record.pop("execution_envelope_sha256")
    state_path.write_text(json.dumps(state), encoding="utf-8")
    committed = (output / "final.wav").read_bytes()

    with pytest.raises(RuntimeError, match="RESUME_PROOF_MISSING_EXPLICIT_RETRY_REQUIRED"):
        do_job(manifest, output, resume=True)
    assert len(posts) == 1 and (output / "final.wav").read_bytes() == committed

    assert do_job(manifest, output, resume=True, allow_retry=True)["state"] == "PASS"
    assert len(posts) == 2


def test_tampered_stored_envelope_cannot_use_existing_final(tmp_path, monkeypatch):
    posts = _wire_assembly(monkeypatch)
    manifest = _manifest(tmp_path)
    output = tmp_path / "out"
    do_job(manifest, output)
    tracked = [output / name for name in ("final.wav", "summary.json", "final-commit.json")]
    before = [path.read_bytes() for path in tracked]
    state_path = output / "resume-manifest.json"
    state = json.loads(state_path.read_text("utf-8"))
    state["sections"]["one"]["execution_envelope"]["headers"]["X-TTS-Backend"] = "MOCK"
    envelope = state["sections"]["one"]["execution_envelope"]
    state["sections"]["one"]["execution_envelope_sha256"] = hashlib.sha256(
        canonical_json_bytes(envelope)).hexdigest()
    state_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(RuntimeError, match="RESUME_PROOF_INVALID_EXPLICIT_RETRY_REQUIRED"):
        do_job(manifest, output, resume=True)
    assert len(posts) == 1 and [path.read_bytes() for path in tracked] == before


def test_oversized_resume_manifest_fails_before_post_and_preserves_final(tmp_path, monkeypatch):
    posts = _wire_assembly(monkeypatch)
    manifest = _manifest(tmp_path)
    output = tmp_path / "out"
    do_job(manifest, output)
    before = (output / "final.wav").read_bytes()
    (output / "resume-manifest.json").write_bytes(b"{" + b" " * (3 * 1024 * 1024))
    with pytest.raises(RuntimeError, match="RESUME_MANIFEST_TOO_LARGE"):
        do_job(manifest, output, resume=True)
    assert len(posts) == 1 and (output / "final.wav").read_bytes() == before


@pytest.mark.parametrize("mutate", [
    _delete("X-TTS-Backend"),
    _replace("X-TTS-Backend", "MOCK"),
    _delete("X-TTS-Execution-Receipt"),
    _delete("X-TTS-Execution-Proof"),
    _replace("X-TTS-Execution-Receipt", "malformed"),
    _replace("Content-Type", "text/plain"),
])
def test_benchmark_rejects_unverified_http_and_writes_no_wav(tmp_path, monkeypatch, mutate):
    import lite.chatterbox_benchmark as benchmark

    monkeypatch.setattr(benchmark, "http_json", _profile_http)
    monkeypatch.setattr(
        benchmark.urllib.request, "urlopen",
        lambda request, **_kwargs: _response(json.loads(request.data), mutate),
    )
    record = run_case("bad", "Benchmark text.", PROFILE["id"], "http://local", tmp_path)
    assert record["state"] == "FAIL"
    assert not (tmp_path / "bad.wav").exists()


def test_benchmark_valid_receipt_is_required_before_wav_publish(tmp_path, monkeypatch):
    import lite.chatterbox_benchmark as benchmark

    monkeypatch.setattr(benchmark, "http_json", _profile_http)
    monkeypatch.setattr(
        benchmark.urllib.request, "urlopen",
        lambda request, **_kwargs: _response(json.loads(request.data)),
    )
    record = run_case("good", "Benchmark text.", PROFILE["id"], "http://local", tmp_path)
    assert record["state"] == "PASS"
    assert record["execution_receipt_verified"] is True
    assert record["backend"] == "CUDA0"
    assert (tmp_path / "good.wav").is_file()
