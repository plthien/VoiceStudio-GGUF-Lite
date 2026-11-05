"""Canonical producer-owned Nano Lite API v1 receipt helpers.

This module is intentionally free of Torch/CUDA imports.  It validates the
self-reported lineage emitted by a worker result; it is not a signature or a
hardware attestation.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from decimal import Decimal, ROUND_HALF_EVEN

from .chatterbox_config import (
    MODEL_HASHES, MODEL_ID, MODEL_REPO, MODEL_REVISION, SOURCE_REVISION,
)
from .chatterbox_segmentation import POLICY_VERSION
from .pause import PAUSE_POLICY_VERSION

CONTRACT_VERSION = "nano_lite_api_v1"
RECEIPT_VERSION = "nano_execution_receipt_v1"
SPEED_POLICY_VERSION = "nano_atempo_speech_only_v1"
MAX_RECEIPT_JSON_BYTES = 6144
MAX_RECEIPT_HEADER_BYTES = 8192
HEX64 = re.compile(r"^[a-f0-9]{64}$")

RECEIPT_FIELDS = frozenset({
    "schema_version", "model", "backend", "repo_id", "model_revision",
    "source_revision", "loader_mode", "weights_manifest_sha256",
    "voice_id", "voice_version", "reference_sha256", "reference_binding",
    "input_sha256", "seed", "speed_mode", "speed_policy_version",
    "requested_speed", "effective_speed", "atempo_applied",
    "pause_policy_version", "segmentation_policy", "speech_span_count",
    "native_chunk_count", "native_chunk_sha256", "wav_sha256",
    "sample_rate_hz", "channels", "sample_width_bytes", "audio_frames",
    "speech_frames", "authored_pause_frames", "pause_count",
})


def canonical_json_bytes(value: object) -> bytes:
    """RFC-8259 JSON subset used by this contract: sorted, compact and finite."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def weights_manifest_sha256(hashes: dict[str, str] | None = None) -> str:
    selected = MODEL_HASHES if hashes is None else hashes
    if (set(selected) != set(MODEL_HASHES)
            or any(not isinstance(k, str) or not isinstance(v, str)
                   or not HEX64.fullmatch(v) for k, v in selected.items())):
        raise ValueError("INVALID_WEIGHTS_MANIFEST")
    return hashlib.sha256(canonical_json_bytes(selected)).hexdigest()


def speed_ascii(value: float) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError("INVALID_SPEED")
    rounded = Decimal(str(value)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN)
    if not Decimal("0.750000") <= rounded <= Decimal("1.250000"):
        raise ValueError("INVALID_SPEED")
    return format(rounded, ".6f")


def frame_seconds(frames: int) -> str:
    if type(frames) is not int or frames < 0:
        raise ValueError("INVALID_FRAME_COUNT")
    return format(Decimal(frames) / Decimal(24000), ".9f")


def build_receipt(*, profile: dict, reference_binding: str, input_text: str,
                  seed: int | None, speed: float, wav: bytes, info: dict) -> tuple[dict, bytes, str, str]:
    """Build and self-check a bounded receipt from verified producer output."""
    speed_text = speed_ascii(speed)
    chunk_hashes = info.get("chunk_text_sha256")
    if (not isinstance(chunk_hashes, list) or not chunk_hashes
            or any(not isinstance(v, str) or not HEX64.fullmatch(v) for v in chunk_hashes)):
        raise ValueError("INVALID_CHUNK_LINEAGE")
    audio_frames = info.get("audio_frames")
    speech_frames = info.get("speech_frames")
    pause_frames = info.get("pause_frames")
    pause_count = info.get("pause_count")
    span_count = info.get("speech_segments")
    native_count = info.get("native_speech_segments")
    integers = (audio_frames, speech_frames, pause_frames, pause_count, span_count, native_count)
    if any(type(v) is not int or v < 0 for v in integers):
        raise ValueError("INVALID_RECEIPT_COUNTS")
    if audio_frames != speech_frames + pause_frames or native_count != len(chunk_hashes):
        raise ValueError("INVALID_RECEIPT_COUNTS")
    if info.get("model") != MODEL_ID or info.get("backend") != "CUDA0" or info.get("model_revision") != MODEL_REVISION:
        raise ValueError("UNVERIFIED_WORKER_IDENTITY")
    if not isinstance(wav, bytes) or not wav:
        raise ValueError("INVALID_WAV")
    if (not isinstance(reference_binding, str) or not HEX64.fullmatch(reference_binding)
            or not isinstance(profile.get("ref_sha256"), str)
            or not HEX64.fullmatch(profile["ref_sha256"].lower())):
        raise ValueError("INVALID_REFERENCE_IDENTITY")
    if seed is not None and (type(seed) is not int or not 0 <= seed <= 2147483647):
        raise ValueError("INVALID_SEED")
    receipt = {
        "schema_version": RECEIPT_VERSION,
        "model": MODEL_ID,
        "backend": "CUDA0",
        "repo_id": MODEL_REPO,
        "model_revision": MODEL_REVISION,
        "source_revision": SOURCE_REVISION,
        "loader_mode": "nano=True",
        "weights_manifest_sha256": weights_manifest_sha256(),
        "voice_id": profile["id"],
        "voice_version": profile["version"],
        "reference_sha256": profile["ref_sha256"].lower(),
        "reference_binding": reference_binding,
        "input_sha256": hashlib.sha256(input_text.encode("utf-8")).hexdigest(),
        "seed": seed,
        "speed_mode": "postprocess_atempo",
        "speed_policy_version": SPEED_POLICY_VERSION,
        "requested_speed": speed_text,
        "effective_speed": speed_text,
        "atempo_applied": speed_text != "1.000000",
        "pause_policy_version": PAUSE_POLICY_VERSION,
        "segmentation_policy": POLICY_VERSION,
        "speech_span_count": span_count,
        "native_chunk_count": native_count,
        "native_chunk_sha256": chunk_hashes,
        "wav_sha256": hashlib.sha256(wav).hexdigest(),
        "sample_rate_hz": 24000,
        "channels": 1,
        "sample_width_bytes": 2,
        "audio_frames": audio_frames,
        "speech_frames": speech_frames,
        "authored_pause_frames": pause_frames,
        "pause_count": pause_count,
    }
    raw = canonical_json_bytes(receipt)
    token = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    proof = hashlib.sha256(raw).hexdigest()
    if len(raw) > MAX_RECEIPT_JSON_BYTES or len(token.encode("ascii")) > MAX_RECEIPT_HEADER_BYTES:
        raise ValueError("EXECUTION_RECEIPT_TOO_LARGE")
    return receipt, raw, token, proof


def decode_receipt(token: str, *, proof: str | None = None, wav: bytes | None = None,
                   expected: dict | None = None) -> dict:
    if not isinstance(token, str) or not token or len(token.encode("ascii", "strict")) > MAX_RECEIPT_HEADER_BYTES:
        raise ValueError("INVALID_EXECUTION_RECEIPT")
    if "=" in token or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        raise ValueError("INVALID_EXECUTION_RECEIPT")
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        if len(raw) > MAX_RECEIPT_JSON_BYTES:
            raise ValueError()
        receipt = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("INVALID_EXECUTION_RECEIPT") from exc
    if (not isinstance(receipt, dict) or set(receipt) != RECEIPT_FIELDS
            or canonical_json_bytes(receipt) != raw
            or receipt.get("schema_version") != RECEIPT_VERSION):
        raise ValueError("INVALID_EXECUTION_RECEIPT")
    fixed = {
        "model": MODEL_ID, "backend": "CUDA0", "repo_id": MODEL_REPO,
        "model_revision": MODEL_REVISION, "source_revision": SOURCE_REVISION,
        "loader_mode": "nano=True", "weights_manifest_sha256": weights_manifest_sha256(),
        "speed_mode": "postprocess_atempo", "speed_policy_version": SPEED_POLICY_VERSION,
        "pause_policy_version": PAUSE_POLICY_VERSION, "segmentation_policy": POLICY_VERSION,
        "sample_rate_hz": 24000, "channels": 1, "sample_width_bytes": 2,
    }
    if any(receipt.get(key) != value for key, value in fixed.items()):
        raise ValueError("EXECUTION_IDENTITY_MISMATCH")
    for key in ("reference_sha256", "reference_binding", "input_sha256", "wav_sha256"):
        if not isinstance(receipt.get(key), str) or not HEX64.fullmatch(receipt[key]):
            raise ValueError("INVALID_EXECUTION_RECEIPT")
    chunks = receipt.get("native_chunk_sha256")
    integer_fields = ("voice_version", "speech_span_count", "native_chunk_count",
                      "audio_frames", "speech_frames", "authored_pause_frames", "pause_count")
    if (any(type(receipt.get(key)) is not int or receipt[key] < (1 if key in ("voice_version", "speech_span_count", "native_chunk_count", "audio_frames", "speech_frames") else 0)
            for key in integer_fields)
            or not isinstance(chunks, list)
            or any(not isinstance(v, str) or not HEX64.fullmatch(v) for v in chunks)
            or receipt["native_chunk_count"] != len(chunks)
            or receipt["audio_frames"] != receipt["speech_frames"] + receipt["authored_pause_frames"]
            or type(receipt.get("pause_count")) is not int):
        raise ValueError("INVALID_EXECUTION_COUNTS")
    requested = receipt.get("requested_speed")
    if (not isinstance(requested, str) or not re.fullmatch(r"[0-9]+\.[0-9]{6}", requested)
            or not Decimal("0.750000") <= Decimal(requested) <= Decimal("1.250000")
            or receipt.get("effective_speed") != requested
            or type(receipt.get("atempo_applied")) is not bool
            or receipt["atempo_applied"] != (requested != "1.000000")):
        raise ValueError("INVALID_EXECUTION_SPEED")
    if receipt.get("seed") is not None and (type(receipt["seed"]) is not int or not 0 <= receipt["seed"] <= 2147483647):
        raise ValueError("INVALID_EXECUTION_SEED")
    if proof is not None and (not HEX64.fullmatch(proof) or not hashlib.sha256(raw).hexdigest() == proof):
        raise ValueError("EXECUTION_PROOF_MISMATCH")
    if wav is not None:
        from .audio import wav_info
        info = wav_info(wav)
        if (hashlib.sha256(wav).hexdigest() != receipt.get("wav_sha256")
                or info["frames"] != receipt["audio_frames"]):
            raise ValueError("EXECUTION_WAV_MISMATCH")
    if expected is not None and any(receipt.get(key) != value for key, value in expected.items()):
        raise ValueError("EXECUTION_CONTEXT_MISMATCH")
    return receipt


def verify_http_response(headers, wav: bytes, *, expected: dict | None = None) -> dict:
    """Verify the complete producer envelope before publishing its WAV.

    ``headers`` may be an HTTPMessage or a case-insensitive response mapping.
    The receipt remains the canonical frame authority; duplicated HTTP fields
    are required to agree so a proxy/client cannot silently publish a response
    with altered type, duration, or span/chunk accounting.
    """
    def get_header(name: str):
        value = headers.get(name)
        if value is not None:
            return value
        return headers.get(name.lower())

    content_type = (get_header("Content-Type") or "").split(";", 1)[0].strip().lower()
    if content_type != "audio/wav":
        raise ValueError("EXECUTION_CONTENT_TYPE_MISMATCH")
    token = get_header("X-TTS-Execution-Receipt")
    proof = get_header("X-TTS-Execution-Proof")
    if not token or not proof:
        raise ValueError("EXECUTION_RECEIPT_MISSING")
    receipt = decode_receipt(token, proof=proof, wav=wav, expected=expected)
    required = {
        "X-TTS-Engine": receipt["model"],
        "X-TTS-Backend": receipt["backend"],
        "X-TTS-Model-Revision": receipt["model_revision"],
        "X-TTS-Voice-Version": str(receipt["voice_version"]),
        "X-TTS-Requested-Speed": receipt["requested_speed"],
        "X-TTS-Effective-Speed": receipt["effective_speed"],
        "X-TTS-Audio-S": frame_seconds(receipt["audio_frames"]),
        "X-Lite-Audio-S": frame_seconds(receipt["audio_frames"]),
        "X-Lite-Speech-S": frame_seconds(receipt["speech_frames"]),
        "X-Lite-Pause-S": frame_seconds(receipt["authored_pause_frames"]),
        "X-Lite-Pause-Count": str(receipt["pause_count"]),
        "X-Lite-Speech-Segments": str(receipt["speech_span_count"]),
        "X-TTS-Native-Chunks": str(receipt["native_chunk_count"]),
        "X-TTS-Segmentation-Policy": receipt["segmentation_policy"],
    }
    if any(get_header(name) != value for name, value in required.items()):
        raise ValueError("EXECUTION_HEADER_MISMATCH")
    return receipt
