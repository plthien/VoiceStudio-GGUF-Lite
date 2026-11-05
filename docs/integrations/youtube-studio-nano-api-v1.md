# YouTube Studio handoff: VoiceStudio Nano API v1

This is the localhost contract for a future YouTube Studio adapter. It does not modify Studio PR #26. Base URL: `http://127.0.0.1:3901`.

`GET /health` is liveness only. `GET /lite/readiness` reports verified installation eligibility separately from worker residency. Cheap polling endpoints never load CUDA or rehash multi-gigabyte weights. Discovery includes `/lite/model-capability`, `/lite/speed-capability`, `/lite/pause-capability`, `/lite/status`, `/lite/worker/status` and `/v1/audio/voices`.

The immutable identity is `model=chatterbox-nano`, `backend=CUDA0`, model revision `71ccd1d0081b430592cea481f4307e764e07bc64`, source revision `ab55923c27757c90cfcc407baa57dc25a7d25217`, loader mode `nano=True`. Never expect Vulkan0, Q8_0 or native estimator-speed proofs.

```json
{"model":"chatterbox-nano","voice":"OWNED_PROFILE_ID","input":"He opened the door. [pause 800ms] Something moved.","response_format":"wav","language":"en","speed":1.15,"seed":42}
```

`POST /v1/audio/speech` returns one validated 24 kHz mono PCM16 WAV. Native generations use sentence boundaries, target 240 and hard maximum 300 spoken characters. A single sentence over 300 is rejected before worker admission. Speed 0.75–1.25 is FFmpeg atempo applied to speech only. Pause markers are authored zero PCM and are not stretched.

Response headers include engine/backend/model revision, voice version, requested/effective speed, elapsed/audio/RTF, speech/final WPM telemetry, independent speech/pause durations, native segment count, scoped CUDA measurements when observed, segmentation policy and `X-TTS-Execution-Proof`. Freeze profile identity before POST and compare it afterward.

422 and 429 are definite non-admission. 403/404/409 are rights or identity failures. 503 is model/CUDA/probe unavailable. 499/504 or a lost response after admission is uncertain and must not be automatically replayed. Error bodies are `{ "error": { "type", "code", "message" } }` and contain no WAV.

Profile CRUD is `/profiles`, `/profiles/{id}`, `/profiles/{id}/audio`, `/profiles/{id}/reference`, `/profiles/{id}/integrity/verify` and `/v1/audio/voices`. Remote URLs and filesystem paths are not accepted. OpenAPI is at `/openapi.json`.

```powershell
python -m pytest tests/chatterbox/test_nano_provider_contract.py -q
```
