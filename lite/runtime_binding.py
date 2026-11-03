"""Sanitized immutable long-form runtime identity; never expose private paths."""
from __future__ import annotations

import hashlib
import json
import os

from .config import MODEL_REVISION, RUNTIME_REVISION, Settings
from .resident_worker import persistent_capability
from .runtime import sha256


def binding_snapshot(settings: Settings, worker_status: dict) -> dict:
    cap = persistent_capability(settings)
    if not cap.get("supported"):
        return {"schema": "lite-longform-runtime-binding-v1", "status": "unavailable"}
    steps_raw = os.environ.get("GGUF_LITE_EXPERIMENT_MASKGIT_STEPS", "32")
    fa_raw = os.environ.get("GGUF_LITE_EXPERIMENT_FA", "baseline")
    cache_raw = os.environ.get("GGUF_LITE_EXPERIMENT_REF_CACHE", "0")
    if steps_raw not in {"32", "24", "16", "12", "8"}:
        raise ValueError("invalid MaskGIT steps")
    if fa_raw not in {"", "baseline", "0", "1"}:
        raise ValueError("invalid flash attention control")
    if cache_raw not in {"0", "1"}:
        raise ValueError("invalid reference cache control")
    steps = int(steps_raw)
    fa = 0 if fa_raw == "0" else 1
    candidate = {"kind": "verified_baseline", "receipt_sha256": None,
                 "flavor": "baseline"}
    if cap.get("performance_protocol"):
        receipt = settings.bin_dir / "performance-candidate.json"
        candidate_data = json.loads(receipt.read_text(encoding="utf-8"))
        flavor = candidate_data["candidate_flavor"]
        if flavor.startswith("steps-") and int(flavor[6:]) != steps:
            raise ValueError("MaskGIT candidate flavor conflicts with effective startup setting")
        if flavor == "fa-off" and fa != 0:
            raise ValueError("FA candidate flavor conflicts with effective startup setting")
        candidate = {"kind": "verified_candidate",
                     "receipt_sha256": sha256(receipt), "flavor": flavor}
    core = {
        "schema": "lite-longform-runtime-binding-v1",
        "exe_sha256": cap["exe_sha256"],
        "candidate": candidate,
        "model_revision": MODEL_REVISION, "native_revision": RUNTIME_REVISION,
        "quant": "Q8_0", "declared_backend": "Vulkan0",
        "protocol": "LitePersistent/v1", "abi": "abi-v2",
        "performance_protocol": bool(cap.get("performance_protocol")),
        "maskgit_steps": steps, "flash_attention": fa,
        "ref_cache_enabled": cache_raw == "1",
    }
    identity = hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    perf = worker_status.get("performance")
    proof = worker_status.get("native_proof") or {}
    verified = bool(
        isinstance(perf, dict)
        and perf.get("maskgit_steps") == steps
        and perf.get("flash_attention") == fa
        and proof.get("verification_status") == "verified"
        and proof.get("observed_native_backend") == "Vulkan0"
        and "GTX 1650" in str(proof.get("observed_device_identity"))
        and cap.get("performance_protocol")
    )
    # Existing LitePerf/v1 does not explicitly attest cache enablement: keep
    # that limitation visible rather than presenting startup env as observation.
    return {
        "schema": core["schema"], "status": "verified_capability",
        "identity_sha256": identity, "exe_sha256": cap["exe_sha256"],
        "candidate_kind": candidate["kind"],
        "candidate_flavor": candidate["flavor"],
        "candidate_receipt_sha256": candidate["receipt_sha256"],
        "declared_backend": "Vulkan0",
        "maskgit_steps": steps, "flash_attention": fa,
        "ref_cache_enabled": cache_raw == "1",
        "settings_verification": "observed" if verified else "not_yet_verified",
        "cache_verification": "declared_only",
        "server_timeout_s": settings.timeout,
    }
