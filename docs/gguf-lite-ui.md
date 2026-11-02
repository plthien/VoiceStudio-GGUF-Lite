# GGUF Lite — Local Web UI

Launch with `scripts/run-gguf-lite.ps1` after existing pinned Q8_0 setup, then visit http://127.0.0.1:3900/ui. Same FastAPI service, no JS build, CDN, cloud, or separate port. Keep the host-specific AMD-switchable-graphics workaround within the current PowerShell session only:
```powershell
$env:VK_LOADER_LAYERS_DISABLE="VK_LAYER_AMD_switchable_graphics*"
.\scripts\run-gguf-lite.ps1 -Preflight
.\scripts\run-gguf-lite.ps1
```

## Voice Cloning
Upload an owned WAV/MP3 (maximum 24 MiB and 60s), manually enter the **exact spoken reference transcript**, confirm permission, then create. The new stable ID is selected in TTS. This is reference-conditioned zero-shot synthesis, not training or automatic ASR. Generate a 24 kHz mono PCM16 WAV, validate the native NVIDIA GTX1650 Vulkan0 headers, play or download locally. No native speed slider.

## Library and deletion privacy
Manage metadata/transcript, validate and play the SHA-checked normalized reference, replace audio (same ID, next version). Default legacy DELETE semantics remove only the profile DB record **and retain owned files**. A separate explicitly confirmed `DELETE ?purge=true` attempts to remove only Lite-owned versioned reference files. Cleanup errors do not mean audio was erased; manual reconciliation may be needed. Never remove the old VoiceStudio source.

## Migration
By default on Windows the UI reads `%APPDATA%\\OmniVoice\\omnivoice.db` and `%APPDATA%\\OmniVoice\\voices/` when the legacy app is closed. An optional **server-side** `GGUF_LITE_LEGACY_ROOT` overrides the trusted root; its default files are `omnivoice.db` and `voices/`. If different, optionally configure `GGUF_LITE_LEGACY_DB` and `GGUF_LITE_LEGACY_VOICES` under that same trusted root. Browser requests cannot specify arbitrary disk paths. Stop legacy VoiceStudio before migration; preview is read-only, and Apply imports only one selected stable ID at a time. The existing PowerShell migration CLI is unchanged.

## Runtime & licence
Use manual **Refresh readiness** since this re-hashes multi-GB Q8 model files. Lightweight `/lite/status` shows busy/voice count without running preflight. Exact licence fingerprint and historical 017094... vs 361609... model terms require independent review; clicking acceptance is not a commercial waiver. UI three-request diagnostic preserves 306 characters / seed 42, and is NOT the authoritative `scripts/benchmark-gguf-lite.ps1` with full per-run evidence.

## Owner-only host verification
Browser CI uses deterministic fake native WAV/headers, NOT physical NVIDIA GTX1650. On the actual Windows host, upload a **fresh** reference (different from migrated Ink Explainer), produce verified native WAV with its new profile ID, inspect playback/download and edit/replace/delete. Keep private audio, logs, model weights and native binaries out of public Git.
