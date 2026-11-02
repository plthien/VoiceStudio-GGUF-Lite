# YouTube AI Studio future backend — preserved GGUF Lite HTTP contract

This document does not modify YouTube AI Studio. Its future backend adapter calls `http://127.0.0.1:3900/v1/audio/speech` (blocking POST) from the same Windows machine.

JSON example:
```json
{"model":"omnivoice-gguf","voice":"<stable-profile-id>","input":"spoken text up to 4096 characters","response_format":"wav","speed":1.0,"language":"en","seed":42}
```
Native output is 24000 Hz, mono, PCM16 RIFF WAV; the separate YouTube Studio adapter converts to 48000 Hz canonical PCM16. Optional speed 0.75–1.25 is experimental and native estimator-based ONLY on a patched Lite Vulkan executable verified by the speed capability receipt; the default speed 1.0 still works with old installed binaries. No WPM target/calibration, alignment, word timestamps or durable queue in Lite. Stable IDs come from `GET /v1/audio/voices`; `/lite/readiness` must not be rapidly polled.

Success native proof: `X-Lite-Verified-Backend=Vulkan0`, `X-Lite-Observed-Device=NVIDIA ... GTX 1650`, `X-Lite-Verification-Status=verified`. Optional `X-Lite-Elapsed-S`, `X-Lite-Audio-S`, `X-Lite-RTF` are native server measurements, distinct from any browser stopwatch.

Typed JSON error envelope: `error.message`, `error.type=lite_error`, `error.code`. Existing 403 licence, 404 profile, 409 reference, 422 validation, 429 busy (Retry-After), 499 disconnect, 503 readiness, 504 timeout and 500 native error remain. Never silently retry an ambiguous request. The Lite Web UI uses the exact same API.

A requested non-default speed fails closed with typed `503 NATIVE_SPEED_UNAVAILABLE` unless `GET /lite/speed-capability` verifies the candidate executable SHA, patch SHA, pinned source revision and real `--help` marker. Header `X-Lite-Requested-Speed` is informational, not measured WPM. There is no automatic FFT/FFmpeg time stretch, replay or modification of owner voices.

Precision rule: JSON `speed` accepts every finite value from 0.75 to 1.25 without rounding to 0.05 or .2f. `X-Lite-Requested-Speed` contains the original parsed request number (e.g. 1.004); `X-Lite-Effective-Speed` is the canonical 9-significant-digit representation of the IEEE-754 float32 sent as native `--speed` (e.g. 1.00399995). The UI slider happens to step 0.05, but API callers do not. These are not measured WPM/RTF or guaranteed attained timing.

## Deterministic inline pause (Lite-local API, no Studio code changes)

The unchanged `POST /v1/audio/speech` JSON `input` accepts `[pause]` (350ms), `[pause 800ms]` and `[pause 1.5s]`. Units are ms or s, ASCII-case-insensitive; seconds support up to three decimal places. Durations 1–10000ms, 16 markers, 60000ms total, 4096 input characters including tags. These tags insert precise zero PCM16 samples into the final verified mono/24000Hz WAV. Leading/trailing/adjacent markers are preserved; surrounding boundary whitespace may be stripped only in tagged requests. Unrecognized expression tags remain unchanged (not interpreted). No `pause` JSON field and no second output. Tagged success includes `X-Lite-Pause-Count`, `X-Lite-Pause-S`, `X-Lite-Speech-S`, `X-Lite-Speech-Segments`, `X-Lite-Audio-S` (final), `X-Lite-RTF` (elapsed/final duration), and `X-Lite-Speech-RTF` (elapsed/speech duration). Each speech span uses the same voice/language/native speed with deterministic derived seed `(seed + zero_based_speech_index) % 2147483648` if seed is set; seed=null is nondeterministic. Speed never changes explicit silence duration. Untagged requests use the unchanged native generation/seed/WAV path. Errors: `422 INVALID_PAUSE_MARKER`, `422 PAUSE_LIMIT_EXCEEDED`, `422 INVALID_REQUEST`, and existing 429/499/503/504/500. Studio integration is not implemented here. WPM is an approximate whitespace word count and is not forced alignment; emotions/reactions remain deferred.
