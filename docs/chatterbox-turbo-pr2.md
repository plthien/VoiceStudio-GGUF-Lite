# Chatterbox Turbo — isolated local experiment (PR #2)

## Source, rights and environment

The containing VoiceStudio-GGUF-Lite application preserves its inherited **AGPL-3.0** licence. The separate Resemble AI Chatterbox Turbo model and upstream source are **MIT** (source commit `5de7a54aa4e5e2baadb0182dde554908b48b85c2`; model snapshot `74971fa28ff1cdf9a2cf4b41d102cb31fd32d411`). Do not treat this application as MIT. This PoC does not import the OmniVoice GGUF licence acceptance/provenance rules. Voice cloning requires permission and explicit confirmation for each import or replacement. Do not upload or commit private reference recordings, transcripts, WAVs, or voice IDs. Model inference is localhost-only. This experiment does not qualify as a production or YouTube Studio provider until owner H gates pass.

Dependency sources: standalone FastAPI/uvicorn/pydantic, PyTorch CUDA 2.6.0/torchaudio 2.6.0, Resemble Chatterbox source, pinned Resemble Perth source, FFmpeg, s3tokenizer, transformers, diffusers, conformer, librosa, safetensors and tokenizer dependencies. See `requirements-chatterbox.txt`, source and install scripts; the actual resolved transitive versions are frozen to `private-dependency-lock.txt` on the owner's D: drive after installation, never committed. Wheel compatibility and actual model load are **OWNER TEST PENDING**.

Model files downloaded explicitly: `t3_turbo_v1.safetensors`, `s3gen_meanflow.safetensors`, `ve.safetensors`, `added_tokens.json`, `merges.txt`, `vocab.json`, `special_tokens_map.json`, `tokenizer_config.json`. No `s3gen.safetensors`, no `conds.pt`, and no remote inference/trust_remote_code. Every downloaded file is hashed into a private manifest, with independently expected SHA256 for the two large weights. Worker load revalidates the complete local set. No GitHub Actions downloads weights or PyTorch.

## Owner installation (Windows 10 / GTX 1650 4 GiB)

```powershell
cd D:\VoiceStudio
git switch feat/chatterbox-turbo-provider
# Stop GGUF Lite / unload its persistent Vulkan0 worker before CUDA load.
.\scripts\install-chatterbox-turbo.ps1 -Root 'D:\VoiceStudioBuild\chatterbox-turbo'
.\scripts\install-chatterbox-turbo.ps1 -VerifyOnly
.\scripts\run-chatterbox-turbo.ps1 -Root 'D:\VoiceStudioBuild\chatterbox-turbo' -Port 3901
```

Visit `http://127.0.0.1:3901/ui` and import an authorized reference longer than five seconds (recommended 6–15 seconds). Store data lives in `D:\VoiceStudioBuild\chatterbox-turbo\data`, not in GGUF Lite data or a legacy OmniVoice database. Only the dedicated `.venv-chatterbox` imports Torch; no background CUDA model loads for health/readiness/status calls. Stop the service when leaving the PoC; the worker is reaped on cancellation or service shutdown.

```powershell
Invoke-RestMethod http://127.0.0.1:3901/chatterbox/readiness | ConvertTo-Json -Depth 5
Invoke-RestMethod http://127.0.0.1:3901/chatterbox/runtime/status | ConvertTo-Json -Depth 5
.\scripts\benchmark-chatterbox-turbo.ps1 -VoiceId '<OWNED_VOICE_ID>' -Mode Smoke
.\scripts\benchmark-chatterbox-turbo.ps1 -VoiceId '<OWNED_VOICE_ID>' -Mode ColdWarm
.\scripts\benchmark-chatterbox-turbo.ps1 -VoiceId '<OWNED_VOICE_ID>' -Mode Pause
.\scripts\benchmark-chatterbox-turbo.ps1 -VoiceId '<OWNED_VOICE_ID>' -Mode LongForm -Manifest 'D:\private\narration-sections.json'
```

The last command needs a private JSON manifest such as:

```json
{"voice":"YOUR_VOICE_ID","sections":[
  {"id":"sec_001","input":"Narration one. [pause 250ms] More text.","speed":1.0,"seed":42},
  {"id":"sec_002","input":"Narration two.","speed":1.0,"seed":43}
]}
```

Sections must stay within 4,096 chars, 16 markers and 60 seconds of pause per request. The long-form CLI writes each section, SHA and sanitized resume manifest atomically, then final WAV only after all sections pass. It refuses automatic retry of an uncertain request; re-execution requires explicit `--resume --retry-uncertain` through `python -m lite.chatterbox_assembly`, and never reuses mismatched source hash, voice version, model revision, or output WAV digest.

Speed `0.75–1.25` is **postprocess_atempo** on speech spans, not Chatterbox native inference speed. Silence markers are inserted as exact 24k zero frames afterwards. Token `[laugh]` is passed through to Chatterbox unmodified. WPM is measured from final output duration; automatic verification of English transcript coverage is **not** performed by tests. CUDA determinism across different drivers is not promised.

## Metrics and GO/NO-GO

The benchmark reports `owner-metrics.json` and `owner-metrics.csv` even on failures. Inspect model load, VRAM/peak, cold/warm wall, pause sample counts, voice likeness, intonation and sentence coverage by listening. The actual SLA is **600 seconds of final narration in <= 1,800 seconds wall, final RTF <= 3**, including cold model load, reference conditioning, all section requests, atempo, pauses and assembly; run twice including cold. Repeated warm success alone is not a pass.

H01–H08 (GTX1650 VRAM/inference, cloning quality, A→B→A, ten sections, pauses, speed quality, 600s SLA and release of VRAM after idle unload) are **OWNER TEST PENDING** until measured. If official CUDA fails with `CUDA_OOM`, do not auto-fallback to CPU or label it CUDA0 success. CPU diagnosis, future ONNX/native runtime comparison are separately approved experiments, not active production engines. Stop rather than replacing Turbo with another model without owner consent.

## CI without GPU

```powershell
python -m pip install -r requirements-lite-dev.txt -r requirements-lite-ui-test.txt
python -m playwright install chromium
python -m pytest tests/chatterbox -vv -ra --timeout=35
python -m pytest tests/lite -vv -ra --timeout=35
```

Both Windows and Ubuntu exercise fake workers, FastAPI and browser workflows without downloading the CUDA runtime. These results do **not** substantiate real acoustics, RTF, torch CUDA installation, or 4 GiB compatibility. Revert PR #2 branch to roll back this PoC; no files or API on GGUF Lite port 3900 need changing.

## Explicit CPU-only fallback diagnosis (not a backend switch)

If and only if owner CUDA tests fail (including CUDA_OOM), an optional CLI can compare this **same pinned Turbo** model on CPU. It does not automatically run, cannot satisfy H01/H07, does not expose an HTTP CPU fallback, and requires adequate system RAM.

```powershell
# With original :3901 service stopped, explicit user opt-in:
$env:CHATTERBOX_ROOT='D:\VoiceStudioBuild\chatterbox-turbo'
$env:PYTHONPATH=(Get-Location).Path
& 'D:\VoiceStudioBuild\chatterbox-turbo\.venv-chatterbox\Scripts\python.exe' -m lite.chatterbox_cpu_diagnostic --voice-id '<OWNED_VOICE_ID>' --text-file 'D:\private\short-english-input.txt' --output 'D:\VoiceStudioBuild\chatterbox-turbo\benchmarks\cpu-diagnostic.wav'
```

Output metrics explicitly label `CPU_DIAGNOSTIC_ONLY`, never `CUDA0`. GPU inference requires owner verification; a future optimized Turbo runtime (such as ONNX/native) needs its own evidence and approved follow-up, and is not shipped here. CUDA/no-CUDA mode must not silently change model identity, voice profile or speed/pause contract.

Installer notes: Resemble Perth is installed from pinned commit `ff1c8ac55a976971245cdd53c18d6131ca00d993` with `--no-deps`, and its runtime dependencies (PyYAML, pydub, soundfile, torch/torchaudio, librosa, numpy) are provided explicitly in the isolated environment. Inspect `private-dependency-lock.txt` generated on D:; a successful CI mock run does not verify every Windows CUDA binary wheel.

## Installer evidence and GPU memory

The isolated installer also writes a local `private-dependency-inventory.json` (package name, installed version, publisher-supplied license metadata and classifiers) under D:. The inventory preserves missing/unknown license information without guessing; inspect the exact wheel/FFmpeg build licenses on the owner host before redistribution. This local evidence file is **not committed**.

Use the explicit diagnostics before the first CUDA test:

```powershell
$env:CHATTERBOX_ROOT = 'D:\VoiceStudioBuild\chatterbox-turbo'
$env:PYTHONPATH = (Get-Location).Path
& 'D:\VoiceStudioBuild\chatterbox-turbo\.venv-chatterbox\Scripts\python.exe' -m lite.chatterbox_config --verify --cuda-probe
Invoke-RestMethod http://127.0.0.1:3900/lite/worker/status | ConvertTo-Json -Depth 4
```

For a safe cold load, PR #1 should show `resident=false` and `busy=false` (or be completely stopped). Turbo checks PR #1 at port 3900 before CUDA model load and refuses if it observes a resident/busy worker or an untrusted response; a connection-refused port is treated as PR #1 stopped. This one-way check cannot stop a separately started PR #1 after Turbo has loaded; do not run both simultaneously. Shut down the other service or explicitly unload its worker first.

Owner metrics include true CUDA allocated/reserved/free/peak byte counts when provided by the real worker, and explicitly leave them null/not measured in any backend that does not report them. CI fake-worker values are synthetic and **not** GTX 1650 evidence.

The `-Mode LongForm` benchmark **forces a Turbo worker unload before starting its clock**, even if earlier Smoke/Pause/ColdWarm modes warmed the model. If unload fails, the run records `COLD_UNLOAD_FAILED` instead of silently measuring warm inference. The first section's model load is therefore included in the measured end-to-end elapsed time. `sla_checked=false` means the supplied manifest produced less than 600 seconds and cannot establish H07; do not label that run as a 10-minute performance pass.
