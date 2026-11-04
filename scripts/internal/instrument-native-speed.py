"""LiteNativeSpeed/v1: strictly anchored patch for the frozen omnivoice.cpp source.

Run AFTER pinned checkout, BEFORE CMake. Edits only the Lite staging tree.
ABI-v2 struct layout and original initializer remain unchanged; the new speed
synthesis entrypoint is additive, with an explicit float argument.
No global speed, audio resampling, or time stretch is used.
"""
from __future__ import annotations
import sys
from pathlib import Path

PIN="886fc079838ca7400cb2b42b36e2a65aa1daabe8"
MARKER="LiteNativeSpeed/v1"

def apply(root:Path)->list[str]:
    root=root.resolve(strict=True)
    edits={}
    def read(name):
        path=root/name
        if path.is_symlink() or not path.resolve(strict=True).is_relative_to(root):
            raise SystemExit("Untrusted pinned native source path")
        return path.read_text(encoding="utf-8")
    def replace(name,old,new,expected=1):
        s=edits.get(name,read(name))
        if s.count(old)!=expected:
            raise SystemExit(f"Pinned source drift or already patched: {name}, expected {expected} exact anchor(s)")
        edits[name]=s.replace(old,new)
    def add_include(name,needle):
        replace(name,needle,needle+'\n#include "lite-native-speed.h"')

    header=root/"src/lite-native-speed.h"
    if header.exists(): raise SystemExit("Already patched: lite-native-speed.h exists")
    from_script=Path(__file__).with_name("lite-native-speed.h")
    data=from_script.read_text(encoding="utf-8")
    if MARKER not in data or len(data)<300: raise SystemExit("Missing audited Lite speed helper")
    # Prevalidate all locations before the first file is modified.
    add_include("src/duration-estimator.h","#include <string>")
    replace("src/duration-estimator.h",
      "    return tokens;\n}",
      """    return tokens;
}

// Keep the entire old estimator path for EXACT speed=1.0 parity.
static int duration_estimate_tokens_speed(const std::string & target_text,
                                          const std::string & ref_text,
                                          int ref_audio_tokens,float speed) {
    if (speed==1.0f) return duration_estimate_tokens(target_text,ref_text,ref_audio_tokens);
    const bool has_ref=ref_audio_tokens>0 && !ref_text.empty();
    const std::string rt=has_ref?ref_text:"Nice to meet you.";
    const int reference_tokens=has_ref?ref_audio_tokens:25;
    float estimate=duration_estimate(target_text,rt,(float)reference_tokens);
    return lite_speed_budget(estimate,speed);
}""")
    # ABI-v2 layout/size and initializer must NOT be changed. New API is additive.
    replace("src/omnivoice.h",
      "OV_API enum ov_status ov_synthesize(struct ov_context * ov, const struct ov_tts_params * params, struct ov_audio * out);",
      """OV_API enum ov_status ov_synthesize(struct ov_context * ov, const struct ov_tts_params * params, struct ov_audio * out);

// LiteNativeSpeed/v1: additional symbol; the ABI-v2 struct remains unchanged.
OV_API enum ov_status ov_synthesize_with_speed(struct ov_context * ov,
    const struct ov_tts_params * params, float speed, struct ov_audio * out);""")
    add_include("src/omnivoice.cpp",'#include "omnivoice.h"')
    replace("src/omnivoice.cpp",
      "enum ov_status ov_synthesize(struct ov_context * ov, const struct ov_tts_params * params, struct ov_audio * out) {",
      """static enum ov_status lite_ov_synthesize_impl(struct ov_context * ov,
    const struct ov_tts_params * params, struct ov_audio * out, float lite_speed) {""")
    replace("src/omnivoice.cpp",
      '    if (!ov || !params) {\n        ov_set_error("ov_synthesize: ov / params is NULL");',
      """    if (!lite_speed_valid(lite_speed)) {
        ov_set_error("Invalid Lite native speed (finite 0.75..1.25 required)");
        if (out) ov_audio_free(out);
        return OV_STATUS_INVALID_PARAMS;
    }
    if (!ov || !params) {
        ov_set_error("ov_synthesize: ov / params is NULL");""")
    replace("src/omnivoice.cpp",
      "return pipeline_tts_synthesize(&ov->pt, &ov->pc, &ov->tok, &ov->vd, params, out);",
      "return pipeline_tts_synthesize(&ov->pt, &ov->pc, &ov->tok, &ov->vd, params, out, lite_speed);")
    replace("src/omnivoice.cpp",
      "int ov_duration_sec_to_tokens(const struct ov_context * ov, float duration_sec) {",
      """// This exact legacy entrypoint never accesses new parameter memory.
enum ov_status ov_synthesize(struct ov_context * ov,
    const struct ov_tts_params * params, struct ov_audio * out) {
    return lite_ov_synthesize_impl(ov, params, out, 1.0f);
}

enum ov_status ov_synthesize_with_speed(struct ov_context * ov,
    const struct ov_tts_params * params, float speed, struct ov_audio * out) {
    return lite_ov_synthesize_impl(ov, params, out, speed);
}

int ov_duration_sec_to_tokens(const struct ov_context * ov, float duration_sec) {""")
    replace("src/pipeline-tts.h",
      "                                  const ov_tts_params * params,\n                                  ov_audio *            out);",
      "                                  const ov_tts_params * params,\n                                  ov_audio *            out,\n                                  float                 lite_speed);")

    # Both buffered and streaming synthesis functions: same token budget policy.
    add_include("src/pipeline-tts.cpp",'#include "duration-estimator.h"')
    replace("src/pipeline-tts.cpp",
       "                                                       float                 chunk_threshold_sec,\n                                                       bool                  denoise,",
       "                                                       float                 chunk_threshold_sec,\n                                                       float                 lite_speed,\n                                                       bool                  denoise,")
    replace("src/pipeline-tts.cpp",
       "                                                     float                 chunk_threshold_sec,\n                                                     bool                  denoise,",
       "                                                     float                 chunk_threshold_sec,\n                                                     float                 lite_speed,\n                                                     bool                  denoise,")
    replace("src/pipeline-tts.cpp",
       "duration_estimate_tokens(text, ref_text, ext_ref_T);",
       "duration_estimate_tokens_speed(text, ref_text, ext_ref_T,lite_speed);",2)
    replace("src/pipeline-tts.cpp",
       "duration_estimate_tokens(ct, this_ref_text, this_T);",
       "duration_estimate_tokens_speed(ct, this_ref_text, this_T,lite_speed);",2)
    replace("src/pipeline-tts.cpp",
       "                                  ov_audio *            out) {",
       "                                  ov_audio *            out,\n                                  float                 lite_speed) {")
    replace("src/pipeline-tts.cpp",
       "    // Streaming path: on_chunk emits chunks of post processed audio at the",
       """    // Explicit native speed; no ABI-v3 tail access or global mutable state.
    if (!lite_speed_valid(lite_speed)) {
        ov_set_error("Invalid Lite native speed");
        return OV_STATUS_INVALID_PARAMS;
    }

    // Streaming path: on_chunk emits chunks of post processed audio at the""")

    replace("src/pipeline-tts.cpp",
       "params->chunk_threshold_sec, params->denoise, mg_cfg, synth_ref_text, synth_ref_tokens, synth_ref_T,",
       "params->chunk_threshold_sec, lite_speed, params->denoise, mg_cfg, synth_ref_text, synth_ref_tokens, synth_ref_T,",1)
    replace("src/pipeline-tts.cpp",
       "params->chunk_duration_sec, params->chunk_threshold_sec,\n        params->denoise, mg_cfg, synth_ref_text",
       "params->chunk_duration_sec, params->chunk_threshold_sec,\n        lite_speed, params->denoise, mg_cfg, synth_ref_text")
    # CLI, both streaming and buffered via ov_tts_params; --duration still overrides in pipeline.
    add_include("tools/omnivoice-tts.cpp",'#include "duration-estimator.h"')
    replace("tools/omnivoice-tts.cpp",
       '            "  --duration <sec>        Output duration in seconds (default: estimate from text)\\n"',
       '            "  --duration <sec>        Output duration in seconds (takes priority over speed)\\n"\n            "  --speed <float>         LiteNativeSpeed/v1: 0.75..1.25 (default: 1.0)\\n"')
    replace("tools/omnivoice-tts.cpp",
       "                          float        prompt_duration_sec,\n                          bool         prompt_denoise,",
       "                          float        prompt_duration_sec,\n                          float        prompt_speed,\n                          bool         prompt_denoise,")
    replace("tools/omnivoice-tts.cpp",
       "    float        prompt_duration_sec    = 0.0f;",
       "    float        prompt_duration_sec    = 0.0f;\n    float        prompt_speed           = 1.0f;\n    bool         prompt_speed_seen      = false;")
    replace("tools/omnivoice-tts.cpp",
       '        } else if (strcmp(argv[i], "--duration") == 0 && i + 1 < argc) {',
       '''        } else if (strcmp(argv[i], "--speed") == 0) {
            if (prompt_speed_seen || i+1>=argc || !lite_speed_parse(argv[i+1],&prompt_speed)) {
                fprintf(stderr, "Invalid or repeated --speed (finite 0.75..1.25 required)\\n");
                return 2;
            }
            prompt_speed_seen=true;
            i++;
        } else if (strcmp(argv[i], "--duration") == 0 && i + 1 < argc) {''')
    replace("tools/omnivoice-tts.cpp",
       "                              prompt_instruct, prompt_duration_sec, prompt_denoise, preprocess_prompt,",
       "                              prompt_instruct, prompt_duration_sec, prompt_speed, prompt_denoise, preprocess_prompt,")
    # Both CLI paths pass an explicit float only when non-default; v2 is untouched.
    replace("tools/omnivoice-tts.cpp",
       "ov_status status = ov_synthesize(ov, &params, nullptr);",
       """ov_status status = (prompt_speed == 1.0f)
                ? ov_synthesize(ov, &params, nullptr)
                : ov_synthesize_with_speed(ov, &params, prompt_speed, nullptr);""")
    replace("tools/omnivoice-tts.cpp",
       "if (ov_synthesize(ov, &params, &audio) != OV_STATUS_OK) {",
       """if ((prompt_speed == 1.0f ? ov_synthesize(ov, &params, &audio)
            : ov_synthesize_with_speed(ov, &params, prompt_speed, &audio)) != OV_STATUS_OK) {""")
    # Native debug paths do not synthesize WAV; prevent speed being mistaken as tested there.
    replace("tools/omnivoice-tts.cpp",
       '    if (tts_mode) {\n        return run_tts_via_ov(',
       '''    if (!tts_mode && prompt_speed != 1.0f) {
        fprintf(stderr, "--speed is supported only in native TTS mode\\n");
        return 2;
    }
    if (tts_mode) {
        return run_tts_via_ov(''')

    # Source drift detected before publishing any modifications.
    for name,s in edits.items():
        (root/name).write_text(s,encoding="utf-8")
    header.write_text(data,encoding="utf-8")
    return sorted(edits)+["src/lite-native-speed.h"]

if __name__=="__main__":
    if len(sys.argv)!=2: raise SystemExit("usage: instrument-native-speed.py <pinned-source-root>")
    print("LiteNativeSpeed/v1 patched:",", ".join(apply(Path(sys.argv[1]))))
