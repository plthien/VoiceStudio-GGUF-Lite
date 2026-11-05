# YouTube Studio handoff: Nano Lite producer API v1

This document is normative for the producer on `http://127.0.0.1:3901`.
Contract version is `nano_lite_api_v1`. PR #26 is a downstream consumer and
must adapt to this truthful producer shape; this service does not emulate its
older provisional fixture.

## Frozen identity and discovery

`GET /lite/model-capability` returns `model=chatterbox-nano`, `backend=CUDA0`,
`repo_id=ResembleAI/chatterbox-nano`, model revision
`71ccd1d0081b430592cea481f4307e764e07bc64`, source revision
`ab55923c27757c90cfcc407baa57dc25a7d25217`, `loader_mode=nano=True`, and
the exact eight-entry `weights_sha256` object. `weights_manifest_sha256` is
SHA-256 of UTF-8 JSON for that map using sorted keys and separators `,` and
`:`, with no whitespace. It is derived metadata and never replaces the map.
There is no `loader_revision`, scalar weights hash, Vulkan or native-speed
claim.

Native segmentation is `nano_sentence_v1`, target 240/hard 300 characters.
The API input maximum is 4096 and is segmented before worker admission.
`definite_429_nonadmission=true` means the atomic admission gate did not queue
or run the rejected request.

`GET /lite/speed-capability` defines `postprocess_atempo`, policy
`nano_atempo_speech_only_v1`, range 0.75–1.25. It affects speech only.
`GET /lite/pause-capability` defines `lite_inline_pause_pcm16_v1`, 24 kHz mono
PCM16 zeros, default 350 ms, 1–10000 ms/event, 16 markers and 60000 ms total.
Examples: `[pause]`, `[pause 800ms]`, `[pause 1.5s]`. `[pause 0ms]`,
`[pause 1.0001s]` and `[pause 11s]` are invalid.

`GET /health` is liveness only. `GET /lite/readiness` separately reports
`installation_prerequisites_ready`, `model_manifest_recorded`,
`model_hash_verified`, `cuda_verified`, `worker_resident`, `worker_busy`,
`cold_load_eligible`, `generation_ready`, `runtime_ready` and
`live_generation_verified`. `generation_ready`/`runtime_ready` mean eligible
to attempt a cold load, not observed CUDA hardware. Polls never load CUDA,
run a probe or rehash all weights.

## Profiles and actual-byte identity

`GET /profiles`, `GET /profiles/{id}` and `GET /v1/audio/voices` expose stable
profile metadata without filesystem paths. Consent is exactly `confirmed`.
`POST /profiles/{id}/integrity/verify` hashes the current normalized reference
under atomic profile admission and returns:

```json
{"verified":true,"binding":"64-lowercase-hex","voice_id":"PROFILE_ID","voice_version":3,"reference_sha256":"64-lowercase-hex"}
```

Name-only edits retain version/binding. A confirmed transcript edit or audio
replacement increments the version, changes the opaque binding and invalidates
conditioning cache. `GET /profiles/{id}/reference` is the rights-aware audio
preview; source paths are never public.

## Speech request, counts and durations

```json
{"model":"chatterbox-nano","voice":"PROFILE_ID","input":"He opened the door. [pause 800ms] Something moved.","response_format":"wav","language":"en","speed":1.15,"seed":42}
```

Success is an exact 24 kHz mono PCM16 WAV. `X-Lite-Speech-Segments` counts
authored speech spans; `X-TTS-Native-Chunks` counts model inference calls.
`X-Lite-Audio-S`, `X-Lite-Speech-S`, `X-Lite-Pause-S` serialize exact frame
counts to nine decimal places. `X-Lite-Pause-Count` is always present, including
zero. `audio_frames = speech_frames + authored_pause_frames`. At 48 kHz a
consumer must multiply the 24 kHz frame boundary by exactly two; do not derive
alignment boundaries from rounded milliseconds.

## Versioned execution receipt

`X-TTS-Execution-Receipt` is unpadded base64url of bounded canonical UTF-8 JSON:
sorted keys, minimal separators, no NaN/Infinity. Schema is
`nano_execution_receipt_v1`; encoded header limit is 8192 bytes and JSON limit
is 6144 bytes. `X-TTS-Execution-Proof` is lowercase SHA-256 of those exact JSON
bytes. It is a checksum for diagnostics, not a signature or independent GPU
attestation.

Required receipt fields are defined by `lite.chatterbox_contract.RECEIPT_FIELDS`
and frozen byte-for-byte in
`tests/fixtures/nano_api_v1/golden_receipt.json`. They bind model/source/loader,
the derived eight-file manifest digest, voice/version/reference/binding,
exact input hash, resolved seed, speed and pause/segmentation policies, ordered
native chunk hashes, response WAV hash and exact PCM frame counts. Requested
and effective speed are six-decimal strings such as `"1.150000"`; the request
JSON remains numeric. CUDA0 is included only after the production service has
validated worker-returned model/backend/revision on each native result. Mock
fixtures validate format only and do not prove a GTX 1650 execution.

## Owned cancellation and outcome semantics

A cancellable POST supplies both `X-TTS-Request-Id` (8–128 safe ASCII chars)
and an independent unpadded base64url `X-TTS-Cancel-Token` containing at least
128 random bits. Cancel with:

```json
POST /lite/worker/cancel
{"request_id":"same-request-id","cancel_token":"same-secret-token"}
```

Missing/stale active request is 404, generation without cancellation identity
is 409, and mismatched ownership is 403. Tokens never appear in status, receipt
or logs. A cancellation acknowledgement means cleanup started, not completed.

422 is pre-admission invalid input and 429 is atomically proven non-admission.
403/404/409 are definite profile/rights/identity failures before successful
publication. A 499/504, connection loss, or any transport error after possible
admission is uncertain and must not be automatically replayed. Long-form retry
requires explicit owner action and its durable section/receipt fence.

## Other endpoints

Worker status/settings/unload are `/lite/worker/status`,
`GET|PUT /lite/worker/settings`, and `POST /lite/worker/unload`. Rights,
migration, OpenAPI and UI remain `/lite/rights`, `/lite/migration/preview`,
`/lite/migration/apply`, `/openapi.json`, and `/ui`. All endpoints are loopback
and same-origin only.

Version history: `nano_lite_api_v1` adds structured receipts, exact span/chunk
and frame accounting, owned cancellation and explicit readiness evidence tiers.
