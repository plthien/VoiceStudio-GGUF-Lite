# YouTube AI Studio future backend — preserved GGUF Lite HTTP contract

This document does not modify YouTube AI Studio. Its future backend adapter calls `http://127.0.0.1:3900/v1/audio/speech` (blocking POST) from the same Windows machine.

JSON example:
```json
{"model":"omnivoice-gguf","voice":"<stable-profile-id>","input":"spoken text up to 4096 characters","response_format":"wav","speed":1.0,"language":"en","seed":42}
```
Native output is 24000 Hz, mono, PCM16 RIFF WAV; the separate YouTube Studio adapter converts to 48000 Hz canonical PCM16. No WPM, native speed, alignment, word timestamps or durable queue in Lite. Stable IDs come from `GET /v1/audio/voices`; `/lite/readiness` must not be rapidly polled.

Success native proof: `X-Lite-Verified-Backend=Vulkan0`, `X-Lite-Observed-Device=NVIDIA ... GTX 1650`, `X-Lite-Verification-Status=verified`. Optional `X-Lite-Elapsed-S`, `X-Lite-Audio-S`, `X-Lite-RTF` are native server measurements, distinct from any browser stopwatch.

Typed JSON error envelope: `error.message`, `error.type=lite_error`, `error.code`. Existing 403 licence, 404 profile, 409 reference, 422 validation, 429 busy (Retry-After), 499 disconnect, 503 readiness, 504 timeout and 500 native error remain. Never silently retry an ambiguous request. The Lite Web UI uses the exact same API.
