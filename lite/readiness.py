"""Shared full-service readiness (distinct from the native engine preflight).

The engine quant endpoint reports raw engine readiness. The CLI and HTTP
/ lite/readiness both use these conservative, side-effect-free full gates.
"""
from __future__ import annotations

def complete_readiness(engine: dict, licence: dict, *, store_healthy: bool,
                       ffmpeg_installed: bool) -> dict:
    runtime_ready=bool(engine["ready"])
    terms_ready=bool(licence.get("accepted") and licence.get("provenance_reconciled"))
    tts_ready=runtime_ready and terms_ready and bool(store_healthy)
    cloning_ready=tts_ready and bool(ffmpeg_installed)
    return {**engine, "runtime_ready":runtime_ready,
            "tts_ready":tts_ready, "cloning_ready":cloning_ready,
            "ready":cloning_ready, "ffmpeg":bool(ffmpeg_installed),
            "ffmpeg_installed":bool(ffmpeg_installed),
            "licence":licence, "voice_store":{"healthy":bool(store_healthy)}}
