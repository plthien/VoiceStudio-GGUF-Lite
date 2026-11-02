# VoiceStudio GGUF Lite

Independent localhost-only FastAPI service for pinned Q8_0 OmniVoice on Windows 10 NVIDIA GTX 1650 Vulkan0. No frontend, Electron, Torch, other TTS engines, ASR or cloud.

Install: `scripts/install-gguf-lite.ps1`; pinned native Vulkan-only build: `scripts/build-gguf-lite-vulkan.ps1`; preflight: `scripts/run-gguf-lite.ps1 -Preflight`; launch: `scripts/run-gguf-lite.ps1`.

See [GGUF Lite documentation](docs/gguf-lite.md). Runtime and voice data are stored separately under `D:\VoiceStudioBuild\lite`. No model downloads at startup or generation. Generated audio is WAV 24 kHz mono PCM16. Model and native revision pins are fixed in `lite/config.py`.

Licence: AGPL-3.0 source in LICENSE, GGUF model notice in `lite/resources/model_notice.json`, original licence notice in LICENSE-NOTICE.md. Model terms include CC-BY-NC-4.0, Higgs Audio 2 and Meta Llama 3. No commercial rights or legal provenance clearance implied.

No voice recordings, model weights, executable binaries, benchmark data or secrets are committed.
