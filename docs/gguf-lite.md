# GGUF Lite development-only service

Isolated local API: lite.app:app, 127.0.0.1:3900. No Electron,
Bun, Torch, ASR or cloud inference. Model Q8_0 revision
361609388ae572a820d085185bbbe2a2aac4b30e and pinned native
886fc079838ca7400cb2b42b36e2a65aa1daabe8.

Install with scripts/install-gguf-lite.ps1 and run
scripts/run-gguf-lite.ps1 -Preflight before service launch.
Native runtime build: bash scripts/build-omnivoice-tts.sh
--platform windows-x86_64 --commit-sha 886fc079838ca7400cb2b42b36e2a65aa1daabe8.
The original build script may fall back to CPU; do not accept that output.
Confirm ggml-vulkan.dll, EXE and checksum manifest on Windows.
Preinstall exactly Q8_0 base/tokenizer at the pinned HF revision under
D:\VoiceStudioBuild\models; no Generate-time downloads.
NVIDIA Vulkan0 (GTX 1650) must be independently verified.
On some host configs VK_LOADER_LAYERS_DISABLE can disable AMD
switchable_graphics layers, but validate before enabling host-specifically.

Known legal blocker: model licence registry cites revision 0170941...
but weights use 3616093.... Manual acknowledgement cannot resolve this
unreviewed provenance mismatch. Terms include CC-BY-NC/Higgs/Meta.
No commercial inference or output rights are warranted, and AGPL-3.0
source obligations remain separate.

Native WAV is 24 kHz mono PCM16. Lite does not apply the legacy
trim/master/normalization/watermark chain; quality parity and 3-run
Ink Explainer Windows benchmark are NOT VERIFIED. Prior original
VoiceStudio Q8_0 mean RTF 3.291 is a baseline, not a Lite result.
Never claim native speed/WPM or automatic sentence segmentation.

Use scripts/migrate-gguf-voices.ps1 with explicit LegacyDb,
LegacyVoices and TargetData paths. Default is dry-run; -Apply backs up
the target, never writes legacy files, and requires a nonempty transcript.
Stop on invalid rows.

Rollback: shut down Lite, switch to feat/gguf-q8-lite, launch original
VoiceStudio; D: Lite store is separate from original AppData voice DB.
Do not publish voices, recordings, model weights or compiled binaries.

Deterministic tests: python -m compileall -q lite;
python -m pytest tests/lite -q.
Live gate: scripts/benchmark-gguf-lite.ps1 -VoiceId <actual-ID>
-Repetitions 3 (Windows host only).
