# VOICESTUDIO — VIENEU V3 NANO — ONE-ROUND IMPLEMENTATION REPORT

**Date:** 2026-11-20  
**Repository:** `plthien/VoiceStudio-GGUF-Lite`  
**Base:** `feat/chatterbox-turbo-provider` @ `7792fb47c292d8f0da8a4666d174be4689ac34b3`  
**Work branch:** `feat/vieneu-v3nano-one-round`  
**Strict final status:** `INCOMPLETE_WITH_BLOCKERS` — model, Windows acceptance, Python test suite and independent audio quality not verified in this execution environment. Do not merge on the strength of this report alone.

## Implementation contents

| Area | Files added or changed | Implementation |
|---|---|---|
| Pins, preflight | `lite/vieneu_config.py`, `lite/vieneu_install.py` | SDK source revision, immutable model revision, SHA256 graphs, manifest, CPU-only and offline checks |
| Isolated inference | `lite/vieneu_worker.py`, `lite/vieneu_runtime.py` | Separate Python venv subprocess; CPU/ONNX; serialized inference; kill/restart on cancellation/timeout; worker IPC |
| Vietnamese jobs | `lite/vieneu_segmentation.py` | UTF-8 preserved; sentence-level spans; SDK 140-char native chunks; deterministic authored PCM pauses |
| Voice library | `lite/vieneu_profiles.py` | Provider-owned persistent cloned voice, consent, reference+conditioning hashes, version/replacement, delete and integrity verification |
| API | `lite/vieneu_routes.py`, `lite/chatterbox_app.py` | Separate /vieneu endpoints; model/provenance receipts; no modifications to existing Chatterbox `Speech` validator or `POST /v1/audio/speech` implementation |
| UI | `lite/chatterbox_ui/index.html`, `lite/chatterbox_ui/app.css`, `lite/chatterbox_ui/app.js`, `lite/chatterbox_ui/vieneu.js` | Single existing UI with provider switch; VieNeu cloning, preset/voice library, preview, generation, WAV playback/download, cancel, runtime status |
| Installer | `requirements-vieneu.txt`, `scripts/install-vieneu-v3nano.ps1`, `scripts/verify-vieneu-v3nano.ps1`, `scripts/run-vieneu-v3nano.ps1` | Isolated venv, pinned SDK, predownload of eight graphs+config, offline verification, launch existing UI |
| Tests | `tests/chatterbox/test_vieneu_provider.py`, `tests/chatterbox/test_vieneu_local_acceptance.py` | Offline policy/store/router mock tests and gated real model/Windows acceptance tests |
| Docs | `docs/vieneu-v3nano.md`, this report | Install, contract, provenance, tests, manual gates |

## Execution evidence

| Gate | Status | Evidence / limitation |
|---|---|---|
| Git baseline inspection | PASS | GitHub connector read baseline commit and relevant files, compared API contract code |
| VieNeu upstream SDK inspection | PASS | Inspected source at `6566468cf14974807050495036ab75cd041143f7`, `Vieneu(mode='v3nano')`, encoder and preset interface |
| JS syntax — `app.js` | PASS | Parsed via V8 JavaScript Function constructor on current branch source |
| JS syntax — `vieneu.js` | PASS | Parsed via V8 JavaScript Function constructor on current branch source |
| Chatterbox `Speech` validator text equality to baseline | PASS (static) | Exact-block textual comparison |
| Chatterbox `POST /v1/audio/speech` text equality to baseline | PASS (static) | Exact-block textual comparison |
| Existing single UI entrypoint | PASS (static) | `/ui/assets/app.js` retained; VieNeu extension attached to same UI |
| Windows installer clean install, pinned files and SDK | NOT RUN | Windows 10 x64, PowerShell and model snapshot unavailable |
| Python compile, pytest unit/API suite and build | NOT RUN | Repository checkout cannot be materialized into this shell environment (GitHub DNS unavailable); no claim of test PASS |
| Real CPU/ONNX preset synthesis | BLOCKED | Pinned model weights and Windows runtime unavailable |
| Real cloned voice, persistence after application restart | BLOCKED | Model, permitted voice reference and local acceptance environment unavailable |
| Real 3/8-min long-form output coverage, pauses, speed | BLOCKED | CPU/ONNX runtime and audio listening needed |
| Real cancel, timeout, crash, recovery | BLOCKED | Live worker process and Windows machine required |
| Chatterbox CUDA0 inference, browser regression | NOT RUN | GTX 1650 Windows environment not accessible |
| Model quality and performance cold-load/RTF/RAM/VRAM | BLOCKED | Never infer benchmark from model README or other machine |
| Dependency licenses | PARTIAL | SDK/model Apache-2.0 metadata recorded; installer writes local distribution inventory; owner must review individual dependency licenses |

**Source validation is not a substitute for runtime tests.** The model is upstream-labeled a preview and its voice similarity is weaker than v3 Turbo; generated speech needs listening acceptance.

## Windows command sequence (run in checkout)

```powershell
git fetch origin feat/vieneu-v3nano-one-round
git switch feat/vieneu-v3nano-one-round
.\scripts\install-vieneu-v3nano.ps1 -Smoke
.\scripts\verify-vieneu-v3nano.ps1 -Smoke
.\scripts\run-vieneu-v3nano.ps1 -Port 3901
```

In another PowerShell instance (installed app remains running):

```powershell
python -m pytest -q tests/chatterbox/test_vieneu_provider.py
python -m pytest -q tests/chatterbox
python -m pytest -q tests/lite
$env:VIENEU_MODEL_ACCEPTANCE='1'
$env:VIENEU_BROWSER_E2E='1'
$env:VIENEU_ACCEPTANCE_URL='http://127.0.0.1:3901'
$env:VIENEU_ACCEPTANCE_REF='D:\path\to\consented-voice.wav'
python -m pytest -q tests/chatterbox/test_vieneu_local_acceptance.py -s
```

After the local acceptance script, **restart the app** and manually verify that a saved cloned profile still synthesizes and that Chatterbox CUDA0 speech/receipts and the existing YouTube AI Studio consumer remain unchanged. Additionally test the complete 3-minute and 8-minute outputs by listening, assess any missing syllables, test modified model hashes, missing FFmpeg, actual worker kill and timeout, verify no internet access during generation, and measure load/RTF/CPU/peak RAM/VRAM on the user's own machine.

Only after all relevant K01–K15 gates are genuinely passed, the local record may be changed to `IMPLEMENTED_AND_VERIFIED`. Otherwise retain `INCOMPLETE_WITH_BLOCKERS` and report exact failures without fabricating results.
