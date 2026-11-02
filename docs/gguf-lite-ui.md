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

FFmpeg status is read from the boolean `ffmpeg_installed` returned by both `/lite/readiness` (manual verification) and cheap `/lite/status`. FFmpeg is needed to normalize newly uploaded references; it is not native GPU attestation. The reference replacement checkbox records an explicit acknowledgement per new recording. API clients omitting optional `consent` still work but result in a null/unconfirmed consent record; previous consent is never inherited.

Speech admission is acquired before reading a mutable profile/reference and held through native completion or cancellation. Concurrent profile edits, replacement, deletion and selected migration fail with typed busy responses; the native runner retains its separate single-process lock. No automatic generation retry or durable server queue has been added.

### Replacing a recording with its own words

Choose a new WAV/MP3 and preview it locally, enter the **exact transcript of that new recording** (not the old recording), and separately confirm permission for this new sample. Alternatively explicitly select **same spoken words** to reuse the existing transcript. The new audio, reference text, SHA, version and consent are saved as one admission-owned operation with the stable voice ID unchanged; failed conversion leaves the previous version. The existing HTTP `PUT /profiles/{id}/audio` still accepts legacy clients without the optional `ref_text` (retaining the previous transcript) or `reuse_existing_transcript` field; this legacy choice is **not independently transcript verified** and is never silent in the browser. No ASR or transcript alignment is claimed.

A Runtime UI refresh now calls expensive `/lite/readiness` once, rendering its nested `quant` snapshot; the independent external `/engines/omnivoice-gguf/quants` endpoint still validates engine pins on demand. `runtime_ready` tracks native GPU/Q8 identity; `tts_ready` adds licence/data; `cloning_ready` adds FFmpeg, and `ready` means full cloning readiness (CLI and HTTP). Missing FFmpeg does not imply missing Vulkan0. Screens do not continuously poll large Q8 hashes.

## Inline deterministic pause

In Text to Speech, choose **Insert Pause** (350ms, 500ms, 1s, 1.5s, 3s or custom integer 1–10000ms), then **Insert at caret**; selected narration stays intact and marker is inserted before it. Enter raw `[pause]`, `[pause 800ms]`, or `[pause 1.5s]` directly if preferred. A request permits up to **16 markers**, **60s** total inserted silence, and **4096 characters** including tags. Malformed tags return `422 INVALID_PAUSE_MARKER`; limits return `422 PAUSE_LIMIT_EXCEEDED`. The service generates one verified 24kHz mono PCM16 WAV with exact zero frames (24 frames/ms), no FFmpeg pause processing. Native speed acts on speech only; duration metadata distinguishes speech from inserted silence. Approx speech WPM and final WPM count whitespace-separated spoken words (rougher for non-English), not forced alignment. Expression/emotion/reaction tags are deferred and not processed by this feature.
