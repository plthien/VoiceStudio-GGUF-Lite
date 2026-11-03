# High-quality TTS comparison POC

This directory is isolated from the production service. It provides a model-agnostic adapter contract and fake executable test path only; it does not add routes, UI, runtime activation or model downloads.

`fake` is always available for deterministic tests. `pocket_tts` is reported available only when its Python package can be discovered and the caller explicitly records licence acceptance. `chatterbox` is intentionally `POC_UNAVAILABLE`. User reference audio remains local. A real comparison must record completion, wall time, 24 kHz mono PCM16 output and owner listening decisions for speaker likeness and prosody.
