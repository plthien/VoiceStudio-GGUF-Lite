# Wav2Vec2 forced alignment (free, local ElevenLabs alternative)

Contract version is `wav2vec2_forced_alignment_v1`. This surface lets a
consumer that expects the ElevenLabs Forced Alignment API — concretely
YouTube AI Studio, which hard-requires forced-alignment character timings
as its timing truth for every voice provider — run entirely on local,
free components hosted by this service.

## Routes

`GET /lite/alignment-capability` reports the engine truthfully:
`engine_available` (torchaudio importable), `model_loaded` (the model is
loaded lazily on the first alignment request; capability checks never
load it), the requested and actual device, and the provider route.

`POST /v1/forced-alignment` accepts the ElevenLabs request shape:
multipart form fields `file` (WAV audio) and `text` (the exact spoken
transcript). The response is the ElevenLabs shape:

```json
{"characters": [{"text": "H", "start": 0.02, "end": 0.06}],
 "words": [{"text": "Hello", "start": 0.02, "end": 0.22,
            "type": "word", "loss": 0.31}],
 "loss": 0.42}
```

Guarantees the consumer validates: the character texts concatenate to
the submitted transcript exactly, timings are monotonic and inside the
audio duration. `loss` is the mean per-token CTC negative log-prob
(telemetry only, as with the vendor field).

## Engine

torchaudio's CTC forced alignment over `WAV2VEC2_ASR_BASE_960H`
(PyTorch/torchaudio are already installed by the Chatterbox installer).
The bundle weights (~360 MB) download to the Torch cache on first use.
Alignment is serialized behind its own lock in the API process and is
independent of the TTS worker admission gate, so it neither blocks nor
is blocked by speech generation. Device is CPU by default;
`VOICESTUDIO_ALIGNMENT_DEVICE=cuda` opts into the GPU (falling back to
CPU when CUDA is unavailable).

## Consumer wiring (YouTube AI Studio)

No consumer code change is needed; the studio already supports a base
URL override:

1. Set the studio process env `ELEVENLABS_API_BASE_URL=http://127.0.0.1:3901`.
2. Store any non-empty placeholder key through the studio's ElevenLabs
   credential endpoint (the resolver only requires a configured key;
   this service ignores the `xi-api-key` header).
3. Keep the Nano provider active for synthesis; only the alignment
   calls are redirected here.

## Limitations

- English only: the 960H vocabulary is A–Z plus apostrophe. Digits,
  punctuation and other symbols carry no acoustic token; their character
  timings are interpolated between neighbouring aligned characters.
- Alignment quality is Wav2Vec2's, not ElevenLabs'. On clean TTS audio
  with an exact transcript it is typically tight, but it is a different
  engine and its output should be judged on real sections.
- A Vietnamese model (XLSR) for the VieNeu path is future work; the
  route and response contract would not change.
