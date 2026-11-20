# VieNeu v3 Nano — independent Vietnamese CPU provider

**Integration branch:** `feat/vieneu-v3nano-one-round`  
**Target:** VoiceStudio Chatterbox Nano Lite, Windows 10 x64, local-only.

## Architecture and compatibility

The existing `lite.chatterbox_app` HTTP app continues to serve `127.0.0.1:3901/ui`, `POST /v1/audio/speech` and the unchanged Chatterbox `nano_lite_api_v1` receipt. No second HTTP server, no Docker, cloud inference or paid API.

Select **English — Chatterbox-Nano** or **Tiếng Việt — VieNeu v3 Nano** in the same UI. VieNeu uses a private `.venv-vieneu` Python 3.11 worker, an ONNX CPUExecutionProvider model, and a separate `data/voices` library. It does not use the GTX 1650, CUDA0, or Chatterbox profiles or receipts.

Pinned provenance: VieNeu SDK `3.8.5`, upstream source `6566468cf14974807050495036ab75cd041143f7`, model `pnnbao-ump/VieNeu-TTS-v3-Nano`, snapshot `aba295eb96a6fa6003ebe417cc1f2802a7adc1dc`. Required ONNX graphs include text, duration, vector estimator, codec decoder, speaker encoder, codec encoder and reference encoder; `constants.npz` and `config.json` are required. SHA256 hashes live in `lite/vieneu_config.py`, and the installer records the SHA256 of the pinned configuration in `manifest.json`. SDK and model license are listed as Apache-2.0; the VoiceStudio application retains its existing license.

## Install on Windows 10 (PowerShell)

Run from the repository root. Python 3.11 x64, Git and FFmpeg must be available on PATH. Internet is used during **installation only**.

```powershell
git checkout feat/chatterbox-turbo-provider
# Review the PR and switch to feat/vieneu-v3nano-one-round to test this integration:
git fetch origin feat/vieneu-v3nano-one-round
git switch feat/vieneu-v3nano-one-round
.\scripts\install-vieneu-v3nano.ps1 -Smoke
.\scripts\verify-vieneu-v3nano.ps1 -Smoke
.\scripts\run-vieneu-v3nano.ps1 -Port 3901
```

To verify offline after installation: disconnect the network and run `powershell -ExecutionPolicy Bypass -File .\scripts\verify-vieneu-v3nano.ps1 -Smoke`. A changed model graph, changed SDK source pin, missing FFmpeg, modified dependency snapshot or missing cloning graph must fail verification. No model download is performed by the worker. `-SkipDownload` is only for explicitly staged installations and is not an acceptance PASS until offline verify succeeds.

The launcher uses the **already installed** Chatterbox Python runtime to host the one HTTP service. VieNeu Python/ONNX runs only under `D:\VoiceStudioBuild\vieneu-v3nano\.venv-vieneu`. Existing Chatterbox install and `scripts/run-chatterbox-nano.ps1` remain supported.

## API contract

- `GET /lite/providers` returns both providers.
- `GET /vieneu/capability`, `GET /vieneu/readiness`, `GET /vieneu/worker/status`, `GET /vieneu/rights`.
- `GET /vieneu/presets` lists real SDK preset voices, prefixed with `preset:`.
- `GET /vieneu/profiles`, `GET /vieneu/profiles/{id}`, `GET /vieneu/profiles/{id}/integrity`, `GET /vieneu/profiles/{id}/reference`.
- `POST /vieneu/profiles`: multipart fields `name`, `ref_audio` (WAV/MP3, max 24 MiB, 3–30 seconds) and `consent=confirmed`. Transcript not required.
- `PUT /vieneu/profiles/{id}`: JSON `{"name":"New name"}`. `PUT /vieneu/profiles/{id}/reference`: multipart new `ref_audio` and `consent=confirmed`.
- `POST /vieneu/profiles/{id}/preview`: generate a fixed Vietnamese voice preview. `DELETE /vieneu/profiles/{id}`: delete provider-owned audio, conditioning and metadata.
- `POST /vieneu/v1/audio/speech`: JSON `{"model":"vieneu-v3-nano","language":"vi","voice":"preset:VOICE_ID","input":"Xin chào [pause 800ms] Việt Nam!","response_format":"wav","speed":1.0,"steps":16,"cfg":3.0,"sway":0.0,"seed":42}`.
- `POST /vieneu/worker/cancel` requires matching `request_id`, `cancel_token` from request headers; `POST /vieneu/worker/unload` affects only CPU worker.

VieNeu response headers include `X-TTS-Engine: vieneu-v3-nano`, `X-TTS-Backend: CPUExecutionProvider`, `X-VieNeu-Contract: vieneu_cpu_onnx_api_v1`, `X-VieNeu-Receipt` (base64url JSON) and `X-VieNeu-Receipt-SHA256`. The independent receipt records input/voice/model/conditioning hashes, parameters, frame count, output SHA256 and elapsed time. Do **not** interpret VieNeu receipts as the Chatterbox CUDA proof.

## Cloning and long-form behavior

Reference audio is re-decoded through FFmpeg to 24 kHz mono PCM16. The explicit permission flag is mandatory. SDK `encode_reference(..., denoise=False)` produces speaker embedding and style tokens; VoiceStudio stores both as versioned conditioning. Rename does not invalidate an embedding. Replacing reference increments `voice_version`, changes reference/conditioning hashes and invalidates previous credentials; the original Chatterbox store is never touched. One successful load after restart is a required local gate.

Vietnamese request segmentation preserves punctuation and diacritics, splitting at up to 700 characters for job control while leaving the SDK's default 140-character inference segmentation intact. Pauses `[pause]`, `[pause 800ms]`, `[pause 1.5s]` add exact PCM silence after generation. Native `speed` is used without FFmpeg `atempo`; rate display is explicitly syllables/minute, **not English WPM**. Jobs retain voice ID/version and verify every segment's SHA and output WAV before publication. On cancellation, timeout or crash, the worker is killed and incomplete jobs are not published. Currently there is **no cross-job resume**.

## Commands and acceptance gates

```powershell
# Static/unit checks (use the existing Chatterbox test environment)
python -m pytest -q tests/chatterbox/test_vieneu_provider.py
python -m pytest -q tests/chatterbox
python -m pytest -q tests/lite
# Offline smoke: genuine model weights and CPU runtime required
.\scripts\verify-vieneu-v3nano.ps1 -Smoke
```

Run the following additional **manual Windows model/hardware acceptance** against `http://127.0.0.1:3901/ui`: 10 Vietnamese preset sentences with diacritics, proper names, dates and punctuation; encode from a permitted 5–8 s reference; generate cloned preview and restart; verify delete, rename and replacement; long-form 3-minute and 8-minute text with independently checked audio and no missing segments; 350/800/1500 ms pauses independent of speed; cancel, crash, timeout, missing model, hash mismatch and no-network clone; verify Chatterbox CUDA0 still works and its receipts remain unchanged. Capture actual RTF, load seconds, peak RAM, CPU and VRAM. Listen and grade cloned voice and pronunciation separately—HTTP 200 does not prove quality. Mark the quality gate `QUALITY_GATE_FAILED` if audio is unusable. A genuine **Windows 10 local** PASS is required before claiming integration verified.

### Known acceptance dependencies

Windows 10, installed pinned VieNeu SDK, exact ONNX model weights, usable reference recording, browser automation and CPU performance measurements cannot be asserted from a GitHub-only code edit. The absence of direct runtime testing must be reported as **NOT RUN** or **BLOCKED**, not as PASS.


## Worker exceptions and synthesis smoke (2026-11-20)

The worker previously converted nearly all SDK inference/clone errors into the unhelpful `VIENEU_WORKER_FAILED`, hiding their origins. Worker exceptions now have a private structured `VIE-DIAG` entry in `D:\VoiceStudioBuild\vieneu-v3nano\logs\vieneu-worker.log` with stage, exception class, message and traceback *frame names*, plus a `diagnostic_id` sent to the HTTP runtime (visible via `GET /vieneu/worker/status` as `last_failure_id`). API responses do not expose exception messages, local paths or source text. Review/redact local messages before sharing logs.

Example local inspection after a failed generate or clone:

```powershell
Get-Content 'D:\VoiceStudioBuild\vieneu-v3nano\logs\vieneu-worker.log' -Tail 30
Invoke-RestMethod 'http://127.0.0.1:3901/vieneu/worker/status' | ConvertTo-Json -Depth 5
```

Exceptions during ONNX synthesis, phonemization, reference enrollment, model loading and segment encoding now have stage-specific response codes and matching private diagnostic entries. These codes identify the *failing stage*, not necessarily the underlying dependency or corrupted artifact. Do not declare the original error solved without reproducing and resolving the actual diagnostic.

`verify-vieneu-v3nano.ps1 -Smoke` now exercises **one real Vietnamese preset inference** with production settings (16 steps, CFG=3), additionally validating the resulting finite nonempty audio samples. The previous smoke only loaded model/cloning graphs and could therefore pass despite runtime synthesis failure. This does not validate owner voice cloning or production audio quality.

**Post-change acceptance:** local Windows inference, cloned voice quality and full pytest have **NOT RUN** here. Do not mark these gates PASS without evidence.


## Windows cp1252/UTF-8 IPC repair (2026-11-20)

Owner's private diagnostics provided two concrete failures:
- `presets`: `UnicodeEncodeError` in `vieneu_worker.reply -> cp1252.encode`, because the redirected stdout text wrapper cannot represent Vietnamese preset labels.
- `synthesize`: `UnicodeEncodeError: surrogates not allowed` in `sea_g2p.Normalizer.normalize_batch`, with `\\udc9d` already in the input text. This is characteristic of a UTF-8 byte stream incorrectly decoded via the Windows redirected text stream.

The VieNeu worker IPC now reads **raw stdin bytes** and decodes `utf-8/strict`, validates that parsed JSON contains no escaped lone surrogate characters, and writes **raw UTF-8 bytes** to stdout. It bypasses the locale's text wrappers entirely. Worker launch also sets `PYTHONIOENCODING=utf-8:strict` and `PYTHONUTF8=1` **only for the VieNeu subprocess** (not Chatterbox). Unexpected bad UTF-8 or invalid JSON surrogate returns `INVALID_IPC_ENCODING` rather than reaching the G2P. Local diagnostic stderr uses ASCII-safe JSON so the error reporter itself cannot fail under cp1252.

Unit tests simulate cp1252 redirected streams with a Vietnamese voice name and sentence, including malformed bytes and JSON-escaped lone surrogates. **Windows runtime retest is still required**; installation/model weights do not need changes. Fully stop and restart the server to spawn a new UTF-8 VieNeu worker after updating the branch.
