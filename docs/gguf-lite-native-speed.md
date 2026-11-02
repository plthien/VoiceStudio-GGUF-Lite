# Experimental LiteNativeSpeed/v1 — owner Windows guide

Speed is applied during native GGUF synthesis by scaling estimated audio frame budgets. It is not FFmpeg `atempo`, WAV time-stretch or a sample-rate change. The patched `--speed` range is 0.75–1.25, default 1.0. A nondefault value does not guarantee an exact change in measured WPM or good audio fidelity.

The frozen upstream revision remains ServeurpersoCom/omnivoice.cpp @ 886fc079838ca7400cb2b42b36e2a65aa1daabe8. The Lite source patch and its header are versioned in scripts/internal. The native `ov_tts_params` ABI-v2 layout and `ov_tts_default_params` initializer remain **unchanged**. A new additive `ov_synthesize_with_speed` native symbol takes an explicit speed while legacy `ov_synthesize` remains at 1.0; CI compiles a guarded old-v2 caller against the actual extracted initializer. Explicit `--duration` takes precedence and continues to use single-shot; speed alone never sets T_override or bypasses long-form chunking.

## Candidate build without overwriting existing runtime

On the owner Windows host, at the exact reviewed PR HEAD, retain all existing data, old binaries, Q8 weights, migrated Ink Explainer reference and licence receipts. Use the installed Python 3.11, Git Bash and Vulkan SDK. An empty *candidate* directory is required.

```powershell
$root = "D:\VoiceStudioBuild\lite"
$candidate = "D:\VoiceStudioBuild\candidate-speed-v1"
.\scripts\build-gguf-lite-vulkan.ps1 -Root $root -OutputRuntime $candidate
Get-Content "$candidate\bin\speed-capability.json"
Get-Content "$candidate\bin\checksums.sha256"
& "$candidate\bin\omnivoice-tts-windows-x86_64.exe" --help
```

The builder verifies exact pinned upstream patch anchors, PE EXE/DLL structure, GGML/Vulkan DLLs, SHA manifest, speed receipt and `LiteNativeSpeed/v1` CLI marker. If a source anchor, C++ build, dependency or integrity check fails, it stops without modifying the original runtime. Building a candidate is not evidence of GTX1650 inference.

## Owner-controlled activation and rollback

Stop the Lite service first; port 3900 must no longer accept connections. Pick an UNUSED backup destination on the SAME volume. Explicitly approve the switch; it does not touch models/voice data.

```powershell
$baseline = "D:\VoiceStudioBuild\lite\runtime\bin"
$candidateBin = "D:\VoiceStudioBuild\candidate-speed-v1\bin"
$backup = "D:\VoiceStudioBuild\owner-backups\baseline-bin-before-speed"
.\scripts\switch-gguf-lite-runtime.ps1 -Mode Activate -CandidateBin $candidateBin -ActiveBin $baseline -BackupBin $backup -OwnerApproves
```

After activation, use `run-gguf-lite.ps1 -Preflight`, then start service, inspect `/lite/readiness` and `/lite/speed-capability`, and generate a real native 24 kHz mono PCM16 WAV with `[LiteAttest/v1]` NVIDIA GTX1650 Vulkan0 proof. On hybrid AMD+NVIDIA GPU hosts, set VK_LOADER_LAYERS_DISABLE only for the current PowerShell session and only if required. Never bypass attestation.

If the patched candidate fails, stop Lite and move the candidate `runtime/bin` to an owner-controlled quarantine. With the active bin absent, restore the original bin using:

```powershell
.\scripts\switch-gguf-lite-runtime.ps1 -Mode Rollback -ActiveBin $baseline -BackupBin $backup -OwnerApproves
```

The rollback script refuses to overwrite any existing runtime or backup. No silent deletion, force switch or automatic activation occurs.

## Owner acceptance (not simulated CI evidence)

For one known voice/reference/text and seed=42, generate 0.85x, 1.00x and 1.15x WAVs. Record native attestation, duration, elapsed time, RTF, approximate actual WPM, pronunciation/word coverage, timbre, clipping, VRAM and listening impressions. Test long multi-sentence nondefault speed to verify chunking on actual 4 GiB GTX1650 without OOM.

Also clone a NEW owned reference A with transcript A, then replace with distinct reference B and transcript B and generate with both. Verify stable profile ID/version/SHA/consent without changing original VoiceStudio. No private audio/WAV/weights/logs should be committed.

Historical Ink Explainer speed=1.0 mean RTF ~3.345 is not a comparable speed benchmark. Commercial GGUF/model/output rights are NOT CLEARED; fork CLA remains a separate owner decision. Code CI cannot substitute for these owner gates.

After candidate manifest/receipt verification, activation **executes the candidate EXE with `--help` before moving any bin directories**. It must exit 0 and expose both `LiteNativeSpeed/v1` and `--speed` within an 8-second/32-KiB output bound. Failure leaves the baseline and candidate untouched.
