# VoiceStudio GGUF Lite — License and Attribution Notice

This file describes **this Lite-only repository**, not the upstream full VoiceStudio app.
The complete legal licence for covered application source code is the **unchanged
GNU Affero General Public License version 3** in [LICENSE](LICENSE).

Copyright 2024–present **Palash Debnath and VoiceStudio contributors**.
Lite contains extracted/adapted portions of [debpalash/VoiceStudio](https://github.com/debpalash/VoiceStudio).
Upstream contributors retain their existing credits and copyright.
The upstream VoiceStudio contributor agreement is retained in
[.github/CLA-1.0.md](.github/CLA-1.0.md); maintaining the files here
does **not** independently resolve this fork's CLA/branch-protection requirements.
Proprietary/relicensing requests must be evaluated with appropriate rights owners.

## Source and build scope

This fork distributes the Lite-only `lite/` FastAPI package, `scripts/`
and associated tests. It does **not** ship the upstream Electron application,
full multi-engine backend, model manager, PyAV desktop stack or mobile packages.
The only supported production service is localhost-only GGUF Q8_0 synthesis.

The Windows C++ executable is **built locally, not redistributed in Git**,
from [ServeurpersoCom/omnivoice.cpp](https://github.com/ServeurpersoCom/omnivoice.cpp)
at pinned commit `886fc079838ca7400cb2b42b36e2a65aa1daabe8`.
`scripts/internal/instrument-attestation.py` instruments the pinned native
backend source to log the actual selected GGML backend and device. Build-time
dependencies and third-party libraries retain their own licence terms.

## Model and voice assets are NOT licensed by the application's AGPL

The GGUF model repository is
[Serveurperso/OmniVoice-GGUF](https://huggingface.co/Serveurperso/OmniVoice-GGUF)
at pin `361609388ae572a820d085185bbbe2a2aac4b30e`.
The historical conversion notice references `017094167b5c9ed565a5076ac9b3b93c5ecf5c73`,
a **different revision**. These remain separate until a human reviews the
exact pinned model and all component terms.

Historical model notice labels **CC-BY-NC-4.0; Higgs Audio 2; Meta Llama 3**.
Original credits: OmniVoice by **Xiaomi / k2-fsa (Han Zhu and coauthors)**,
GGUF conversion by **Serveurperso**, tokenizer from **Boson AI / Meta
ecosystem**. See the extracted source notice in
[lite/resources/model_notice.json](lite/resources/model_notice.json)
and exact model pins in
[lite/resources/q8_pins.json](lite/resources/q8_pins.json).

Neither an application licence nor an acknowledged JSON fingerprint gives
commercial permission for restricted GGUF weights, tokenizers, reference
recordings, generated outputs, third-party codecs or use of a person's voice.
Consent and usage rights must be reviewed separately by the relevant owners.
Unknown output rights and provenance are **NOT VERIFIED**; commercial
clearance is **false**, never inferred from a successful test.

The complete terms of AGPL-3.0 govern the covered application source and
the original contributors' rights. This notice is explanatory, not a waiver
or replacement for binding model and component licences. No weights, personal
recordings or native binaries are bundled here.
