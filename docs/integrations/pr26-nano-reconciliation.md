# PR #26 reconciliation against canonical Nano producer v1

This is a read-only handoff. PR #2 is the producer source of truth; PR #26 was
not modified and interoperability E01–E06 remains unverified until its next
implementation round consumes the checked-in golden fixture.

| PR #26 provisional expectation | Canonical PR #2 producer | Required consumer adaptation |
|---|---|---|
| `repository` | `repo_id` | Read `repo_id` |
| scalar `weights_sha256` | eight-file `weights_sha256` map plus derived `weights_manifest_sha256` | Verify every map entry and recompute derived digest |
| `loader_revision` | no such observed fact; `loader_mode=nano=True`, pinned model/source revisions | Remove invented loader revision predicate |
| provisional segmentation/version | `nano_sentence_v1`, 240/300, API max 4096 | Use producer values |
| provisional speed identifier | `nano_atempo_speech_only_v1` | Accept exact producer policy and six-decimal receipt strings |
| provisional pause identifier | `lite_inline_pause_pcm16_v1` | Use producer grammar/bounds and exact frame counts |
| consent in `granted/verified/owned` | `confirmed` | Accept the stored producer value `confirmed` |
| metadata-only reference proof | actual-byte `{verified,binding,voice_id,voice_version,reference_sha256}` | Freeze and compare all returned fields |
| provisional receipt/parser | `nano_execution_receipt_v1` unpadded base64url canonical JSON | Decode fixture, verify checksum, context, WAV hash and counts |
| speech segments treated as chunks | spans in `X-Lite-Speech-Segments`; calls in `X-TTS-Native-Chunks` | Preserve both counters separately |
| global/implicit cancel support | request ID plus independent secret token | Do not advertise cancel until token ownership is implemented |

Consumer tests should start with
`tests/fixtures/nano_api_v1/golden_receipt.json`, then exercise the real
producer. 422 and 429 are proven pre-admission; timeout/499/transport loss is
uncertain and must not trigger an implicit duplicate POST. Producer contract
success does not claim PR #26 integration success or hardware evidence.
