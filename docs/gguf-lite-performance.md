# GGUF Lite performance candidates

The production baseline remains pinned Q8_0 on Vulkan0 with 32 MaskGIT steps, native Speed, deterministic Pause, Voice Cloning, and the existing API/UI. Performance builds are emitted to a uniquely tagged `candidates/performance-q8-vulkan-*` directory and are never activated by a build or benchmark.

Candidate controls are process-level and opt-in:

- `GGUF_LITE_PERF=1` enables the private fixed-size `LitePerf/v1` report.
- `GGUF_LITE_EXPERIMENT_REF_CACHE=1` enables exact single-slot reference conditioning reuse; default is `0`.
- `GGUF_LITE_EXPERIMENT_MASKGIT_STEPS` accepts only `32`, `24`, `16`, `12`, or `8`; default is `32`.
- `GGUF_LITE_EXPERIMENT_FA` accepts `baseline`, `1`, or `0`; default is `baseline`.

`LitePerf/v1` retains its fixed 152-byte wire ABI. The historical
`maskgit_sampling_us` slot is derived as total MaskGIT time minus measured LM
forward time; reports label the same value `maskgit_non_lm_overhead_us` because
it can include orchestration overhead and is not an exclusive sampling timer.

Run `scripts/verify-performance-candidate.ps1 -CandidateBin <candidate>\bin` before any owner-controlled switch. Activation still requires `switch-gguf-lite-runtime.ps1 -OwnerApproves`; rollback preserves the original directory on the same volume. Graph reuse is deliberately `GRAPH_REUSE_DEFERRED`. CUDA is optional and must be reported `CUDA_NOT_AVAILABLE` when its toolchain is absent. Any future Q4 experiment must use a separate quant identity and receipt; it cannot be labelled Q8_0.

For owner measurement, keep the same voice version, text, seed, speed, model hashes and backend, then run cold/warm/warm. Use `benchmark-longform-tts.ps1` for authoritative sections and resumable streaming assembly. To resume an interrupted run, pass its printed output directory back with `-RunDir <directory>` together with the same sections, seed and speed; a resumed run is always reported as ineligible for fresh-run SLA. The script derives and pins the live profile version/reference identity, and an explicitly supplied `-VoiceVersion` must match it. Generated audio, section text and voice identifiers stay in the owner-selected output directory and must not be committed.
