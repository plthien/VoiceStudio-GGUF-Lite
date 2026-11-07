# VoiceStudio Nano Lite

PR #2 ships one active synthesis engine: `chatterbox-nano` (Chatterbox-Nano 110M) on CUDA0. It listens only on `127.0.0.1:3901`; the UI is at `/ui`. The stacked branch still contains GGUF sources inherited from PR #1, but the Nano installer, launcher, Python entrypoint and UI neither import nor start that runtime. Port 3900 is queried read-only solely to prevent unsafe GPU co-residency.

## Immutable installation

```powershell
.\scripts\install-chatterbox-nano.ps1 -Root 'D:\VoiceStudioBuild\chatterbox-nano'
.\scripts\install-chatterbox-nano.ps1 -VerifyOnly
.\scripts\run-chatterbox-nano.ps1
```

The installer pins source `ab55923c27757c90cfcc407baa57dc25a7d25217` and model revision `71ccd1d0081b430592cea481f4307e764e07bc64` from `ResembleAI/chatterbox-nano`. Verification is offline, hashes all eight allowlisted files and rejects missing or changed receipts. Runtime loads only the verified local directory with `from_local(..., device="cuda", nano=True)`. There is no CPU or Turbo fallback.

Native Nano inference uses 520-character target / 600-character hard chunks
(`nano_sentence_v2`), independent of the 4096-character API request guard.
A runtime-reported truncated/failed generation is rejected, not published as WAV.
Bare tensor output cannot by itself prove full narration coverage; owner listening
and longform testing remain required.

Voice profiles live under the Nano-owned `data` directory. Import from an older installation is never automatic: configure `CHATTERBOX_NANO_LEGACY_ROOT`, preview it in Voice Library, then explicitly import one eligible voice. The source is read-only.

## Owner benchmark

```powershell
.\scripts\benchmark-chatterbox-nano.ps1 -VoiceId '<OWNED_ID>' -Mode ColdWarm
.\scripts\benchmark-chatterbox-nano.ps1 -VoiceId '<OWNED_ID>' -Mode All -Manifest 'D:\private\nano-sections.json'
```

Each invocation creates a unique cold-run archive. Duration SLA (at least 600 seconds of audio in at most 1800 seconds, RTF at most 3) and full-manifest coverage are separate results. The final assembled WAV is capped at 900 seconds. GTX 1650 memory, OOM behavior, acoustic quality and real performance remain owner-only gates.

Application code is AGPL-3.0. The pinned Chatterbox source/model reports MIT; this does not relicense VoiceStudio.
