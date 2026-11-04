# PW01 — True ov_context reuse feasibility (owner-host gate)

**Status: NOT VERIFIED for the full resident service.** This repository intentionally keeps its known-good single-request native CLI and does not enable or advertise persistent model residency. The audited pinned native source exposes ov_init, ov_synthesize_with_speed, ov_audio_free and ov_free. Its compute scheduler is reset between synthesis calls, but that static evidence cannot establish GTX1650 4GB memory stability, same-seed A→B→A sound parity, or correct restoration after an error.

This change adds a real C++ A→B→A probe (not a mock worker) compiled from the pinned source after the exact existing LiteAttest and LiteNativeSpeed patches, and an opt-in owner-only separate candidate build. Windows/Ubuntu CI can verify pinned stage anchors and compile the probe against the original ABI-v2 header plus additive Speed API. CI does not have the pinned model files or GTX1650 Vulkan GPU, and therefore cannot run the three actual model inferences or qualify the resident protocol.

## Owner commands (Windows PowerShell, use your own voice A and B)

1. Stop the existing Lite service and other GPU inference. Keep installed bin, model cache, cloned voice library and licence data unchanged.
2. On the PR branch at the reported SHA, build an isolated native candidate, not the active bin:

    .\scripts\build-gguf-lite-vulkan.ps1 -Root 'D:\VoiceStudioBuild\lite' -GitBash 'C:\Program Files\Git\bin\bash.exe' -OutputRuntime 'D:\VoiceStudioBuild\reuse-feasibility-candidate' -BuildFeasibilityProbe

3. Run the probe with two distinct privately owned normalized reference WAVs and exact private transcripts. Pass the verified base/tokenizer file paths already installed in your model cache:

    .\scripts\check-native-residency-feasibility.ps1 -CandidateBin 'D:\VoiceStudioBuild\reuse-feasibility-candidate\bin' -BaseGguf '<absolute verified base Q8_0 GGUF>' -TokenizerGguf '<absolute verified tokenizer Q8_0 GGUF>' -ReferenceAWav '<owned A.wav>' -TranscriptAFile '<owned A.txt>' -ReferenceBWav '<owned B.wav>' -TranscriptBFile '<owned B.txt>' -Seed 42 -Speed 1.0

The host script hashes the exact pinned GGUF pair and candidate EXE/Vulkan DLL, creates private PCM16 input via FFmpeg, then runs the compiled probe with a 15-minute bound. The probe creates ONE actual ov_context, invokes three native syntheses on the same pointer using separate A/B/A reference buffers and seeds (42/43/42), frees each returned ov_audio and calls ov_free once. Failure exits nonzero and leaves the old installed runtime untouched. Success requires repeated A output to match bit-for-bit at PCM16 precision, B to be distinct and exactly one actual NVIDIA GTX1650 Vulkan0 native attestation record. Private stderr may include local paths; do NOT publish raw logs, references, audio or cloned voices.

Output stays in the private D: benchmarks area. This is a feasibility checkpoint, NOT proof of warm performance or stable VRAM. Record sanitized pass/fail category, live memory/VRAM peak and release externally with owner-approved tools. If the host test cannot be completed or fails, do not enable a resident mode or fake it by keeping a parent process alive while reloading models.

## Acceptance mapping / stop

- R01: same existing PR/base, no new PR or automatic merge — code gate can PASS.
- R02: pinned source and ABI-v2 untouched; additive optional probe only — source/CI evidence pending.
- R03/R06/R08/R09/R17/R23: actual repeated synthesis, no state bleed, native attestation and VRAM release — NOT VERIFIED until model + GTX1650 run and compiled full native build evidence.
- R04/R05/R07/R10–R16/R18–R22/R24: no permanent worker, protocol, idle-unload settings or UI has been implemented or verified yet; do NOT mark PASS.
- R25–R27: owner-only long-form warm benchmark, VRAM and listening — NOT VERIFIED.
- R28: existing licence and repository policy remains separate owner responsibility.

**Reason for the stop:** the implementation plan itself requires proving repeat synthesis on one true ov_context *before* proceeding with the persistent process. A C++17 syntax test and static scheduler inspection alone cannot satisfy that safety prerequisite on a host without GPU/model files. This is intentionally not a completed persistent worker feature; keep PR Draft.
