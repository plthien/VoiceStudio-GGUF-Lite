# Pocket TTS V2 implementation report — PARTIAL / BLOCKED

## Repair round (2026-11-27, after independent audit of this PR)

- **F1 FIXED — INT8 contract hole.** `PocketSpeech` previously accepted `precision="int8"` although the registry policy is FP32-only. The contract now rejects any precision outside the resolved model's registry `precision_policy` with `PRECISION_UNAVAILABLE`. Regression tests cover all seven models.
- **F2 PARTIALLY FIXED — asset pins.** Registry entries now carry `config_sha256` (SHA256 of the upstream YAML at tag v4.0.1), `weights_sha256`/`weights_size` (HF LFS metadata for `model.safetensors` at the pinned revision) and `tokenizer_sha256`/`tokenizer_size` (SHA256 of the actually downloaded `tokenizer.json` at its pinned revision). Preset pins are in `lite/pocket_pins.py`: 27 preset embeddings per model from the pinned weights revision; preset names are shared across models but **every hash is model-specific** (verified: 27/27 names have 7 distinct hashes). Still outstanding: Windows dependency lock, gated weights *download* verification, and installer-side enforcement (no installer exists yet).
- **F4 PARTIALLY FIXED — segmentation/normalization record.** Each registry entry now records the pinned config's `sdk_replace_characters` map verbatim (e.g. Spanish SDK strips `¡`/`¿`, French maps `:` to `,`, English only normalizes curly apostrophes). Tests assert the application ledger preserves the original text (SDK normalization is recorded, never applied by the app), plus golden word-count fixtures (de/fr/es) and an abbreviation-integrity case. Still outstanding: a true sentence segmenter, larger per-language abbreviation sets, WPM receipt reporting.
- Tests: `tests/chatterbox/test_pocket_contract.py` is now **45 tests** (40 + 5 repair tests), all passing in the audit sandbox (pydantic 2.14.4, pytest 9.2.0, FFmpeg 8.1.2). Gate impact: P02 moves from BLOCKED to PARTIAL (pins verified; lock/download open); M02 asset-distinctness evidence now exists for weights and presets. All other gate statuses below are unchanged.

Plan: VOICESTUDIO_POCKET_TTS_MULTILINGUAL_ONE_ROUND_IMPLEMENTATION_PLAN_V2.md.
Base: feat/chatterbox-turbo-provider at 53cccb2a8d9da24b48b591eec36c3a75cd2affed (exact match to reviewed baseline).
Branch: feat/pocket-tts-provider, created from that SHA. Tested source head before this report: 0a1bd33a9d7043879b1047972aa519e3f4a7e1d0.

## Verified upstream evidence

- SDK tag v4.0.1 resolved to a922b912dfa366975294afb568d815b305acde5d.
- All seven YAMLs fetched at that commit; model and tokenizer revision URIs, config Git blob identity, expected layer count and sample rate are transcribed in lite/pocket_registry.py. English is 6L; all six other entries are 24L. Config Git blob IDs are explicitly not asset SHA256 hashes.
- PyPI published wheel pocket_tts-4.0.1-py3-none-any.whl SHA256 a6baf5fd9195399a632d7ef1cb75d4ee772b8d3534c85b90164b39c687a69961; PyPI provenance identifies the same source commit. Source: https://pypi.org/project/pocket-tts/4.0.1/#files
- Upstream pyproject requires Python >=3.10,<3.15 and torch>=2.5.0. Full Windows lock, actual installed wheel and CPU build not verified.
- Pinned tts_model.py source confirms load_model(config=...), generate_audio(copy_state=True), get_state_for_audio_prompt(truncate=False), local safetensors state reload. The SDK's weights loader has a fallback to non-cloning weights when configured; any eventual local config must remove weights_path_without_voice_cloning. Local inference and export/reload are not verified.
- Hugging Face revision metadata and exact asset file pages could not be retrieved in this session. Direct git network request failed with 'Could not resolve host: github.com'; GitHub connector could read/write source but cannot provide a complete checkout or run the repository. Complete weights/tokenizer/preset SHA256, exact preset compatibility/license evidence, dependency lock and runtime verification remain blocked. Do not use mutable aliases or claim these are verified.

## Delivered foundation

- lite/pocket_registry.py: seven explicit model tuples and provenance references; FP32 policy only, no production INT8 qualification.
- lite/pocket_contract.py: strict Pydantic request validation; mandatory language/variant, rejection of mismatches, extra fields, bool/non-finite speeds and invalid Unicode.
- lite/pocket_segmentation.py: source-span ledger, 320 character speech limit, seven language abbreviation policies, Unicode text preserved, existing pause grammar and orthographic word count. Numeric punctuation separates tokens; apostrophe/hyphen joined; NFC only for counting. Full grapheme algorithm and language-competent normalization review not verified.
- lite/pocket_audio.py: PCM RIFF/frame/format/integrity validation, clipping policy, bounded assembly, exact PCM pause frames, FFmpeg atempo with bypass at 1.0. This is not connected to a synthesis pipeline. 120 second raw chunk bound is a proposed application guard, not a model capability.
- tests/chatterbox/test_pocket_contract.py: 40 host-only tests, including all seven language tuples, invalid contracts, exact source coverage, pause frames, real FFmpeg speed, odd PCM data rejection and CRLF boundary.

No existing source file was edited. The new modules are not mounted or wired into the host; there is no Pocket endpoint or UI in this partial change. No installer/runtime/profile/receipt implementation or model downloads. Do not install or use this branch as a completed Pocket provider. No PR, merge, or Actions dispatch performed.

## Actual test evidence

Linux sandbox, Pydantic 2.13.5, pytest 9.0.2, system FFmpeg. The five new source/test files and baseline lite/pause.py were reconstructed locally from fetched text and Git blob IDs compared against GitHub. Final three changed blobs matched GitHub update responses after hardening. Command from isolated directory:
python -m pytest -q tests/chatterbox/test_pocket_contract.py
Result: 40 passed in 0.41s.
Initial run: 38 passed in 0.51s; added and passed two hardening tests. Final exact Git blob identities:
- lite/pause.py: 101d18ff3841d0a118d7cf96598b7c2fe4eb405d
- lite/pocket_registry.py: 900570fcbe756c68593b1dda08f563da76077769
- lite/pocket_contract.py: d4bfb13cf389df98ed7d4ad6d05b28708fde6a71
- lite/pocket_segmentation.py: fa54259fd7dd913fcc69c03d6095b16baac1d04a
- lite/pocket_audio.py: 940daa86d67f9bfa3e9fbfcad02736a335ba142b
- tests/chatterbox/test_pocket_contract.py: ab4bb49ed8e3002c551ca9c709a7b63e520165f4

These tests use synthesized PCM fixtures, not Pocket model audio. Not a full checkout, host environment, model inference, hardware benchmark or legacy regression.

## Gate status

- P00 PASS (base SHA and branch creation).
- P01 BLOCKED/incomplete (key source files fetched and outlined; complete source/test inspection unavailable).
- P02–P04 BLOCKED (partial upstream source/package evidence above, missing asset authority, Windows lock, actual local load/clone/preset verification).
- I01–I08, R01–R10, V01–V10 BLOCKED (installer/worker/profile work not delivered).
- L01–L12 BLOCKED as complete gates; isolated lossless segmentation, pause/speed/PCM tests passed, longform inference and integrated assembly not run.
- A01–A14 BLOCKED as complete gates; strict request validation tested; routes, receipt, HTTP errors/security/cancel ownership not delivered.
- U01–U08 BLOCKED (UI not delivered).
- G01–G02 NOT RUN (full suites unavailable).
- G03–G06 NOT RUN (genuine model/Windows/quality acceptance).
- M01–M14 BLOCKED as complete gates; seven exact entries and contract/segmentation test coverage are evidence of subsets only, not full multilingual integration.
- Every language es/fr/de/pt/it/nl/en: offline preset, clone/restart, 3/10 minute quality, FP32 RTF/RAM, INT8 qualification all NOT RUN.

No production readiness claim. This is a partial implementation, not completion of the one-round plan. Remaining work includes exact asset and dependency pin authority, install/verify scripts, isolated worker and IPC/process-tree cleanup, profile/preset management, mounted HTTP routes and receipt, UI integration, docs and full legacy/model/hardware acceptance. Do not merge.
