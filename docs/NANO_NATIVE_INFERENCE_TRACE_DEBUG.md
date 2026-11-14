# Nano native inference trace (opt-in, local-only)

Purpose: diagnose text omissions in generated audio even when the exact HTTP
input is correct. This captures the precise text/seed arriving at
`model.generate()` and the returned **native WAV before FFmpeg atempo**.

The tracing code has **no changes** to model generation, token limits,
native seed derivation, segmentation, audio joining, pacing, or retries.

## Windows setup

Open PowerShell **before launching the VoiceStudio-Nano service** and set:

```powershell
$env:NANO_INFERENCE_TRACE_DIR = 'D:\VoiceStudio\logs\nano-native'
$env:NANO_INFERENCE_TRACE_SAVE_WAV = '1'
# Start the VoiceStudio-Nano service via your normal run command in this terminal.
```

When Nano runs in an already-running process, restart it after setting the
environment variables; the worker process needs to inherit them.

Use the existing YouTube AI Studio request replay, or regenerate only
`sec_007`. The second option sends the exact values of its own operation,
including canonical input, seed, and speed.

Files:

- `nano_native_trace.jsonl`: newline-delimited events, one request-correlated
  `trace_id` each; all records include UTC timestamp and producer process ID.
- `<trace_id>_chunk0001_native.wav`: the native output of the actual
  `model.generate()`, **before** FFmpeg speed processing.
- `<trace_id>_chunk0001_after_atempo.wav`: only when speed != 1.0.
- `<trace_id>_chunk0000_final.wav`: exact WAV returned by producer service.

Events: `request_received` -> `native_chunk_input` ->
`worker_model_generate_input` -> `worker_model_generate_output` ->
`native_chunk_output` -> optional `chunk_after_atempo` ->
`request_completed` or `request_error`.

Core fields:

- `input_text`, `input_sha256`, `request_seed`, `speed`, `voice_id`
- `chunk_index`, `chunk_text`, `chunk_text_sha256`, `derived_seed`
- `native_wav_sha256`, `native_duration_seconds`, `raw_wav_filename`
- `output_wav_sha256`, `final_wav_filename`

In the **same request** compare:

1. `native_chunk_input.chunk_text` vs
   `worker_model_generate_input.chunk_text`.
2. `native_chunk_output.native_wav_sha256` vs
   `worker_model_generate_output.native_wav_sha256`.
3. Listen to `*_native.wav`. If it omits a phrase, the omission is already
   in the native model's audio, not YouTube Studio's assembly/alignment.
4. If native WAV is complete but final WAV is missing words, inspect
   `chunk_after_atempo` and `request_completed`.

PowerShell:

```powershell
$dir = 'D:\VoiceStudio\logs\nano-native'
$events = Get-Content (Join-Path $dir 'nano_native_trace.jsonl') | ConvertFrom-Json
$requests = @($events | Where-Object event -eq 'request_received')
$requests | Select-Object trace_id,voice_id,request_seed,speed,input_characters | Format-Table
$trace = $requests[-1].trace_id
$events | Where-Object trace_id -eq $trace | Format-List
Get-ChildItem $dir -Filter "${trace}*native.wav"
```

**Security:** Logs contain the entire voice text, plus voice and seed IDs;
WAV snapshots contain spoken audio. The trace is disabled when
`NANO_INFERENCE_TRACE_DIR` is unset. WAV saving additionally requires
`NANO_INFERENCE_TRACE_SAVE_WAV=1`. They are written only to your selected
local directory; never check them into Git or publish them unredacted.
Reference-voice WAVs, reference paths, keys and cancellation tokens are not
logged. File creation errors are nonfatal.

To disable, unset both environment variables and restart Nano:

```powershell
Remove-Item Env:\NANO_INFERENCE_TRACE_DIR -ErrorAction SilentlyContinue
Remove-Item Env:\NANO_INFERENCE_TRACE_SAVE_WAV -ErrorAction SilentlyContinue
```

## Tests without CUDA

From repository root on Windows; use a repo-local pytest tmp directory if
your user profile's Temp folder has WinError 5 ACL problems:

```powershell
python -m pytest -q tests/chatterbox/test_nano_native_trace.py tests/chatterbox/test_nano_600_inference.py --basetemp=runtime/pytest_nano_trace
```

`runtime/` is ignored by the repo and pytest may clear the chosen
`--basetemp` contents at each run. These tests use a fake resident
worker; only an owner-run comparison can confirm production CUDA/voice fidelity.
