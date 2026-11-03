"""Fail-closed LitePerf/v1 instrumentation for the exact pinned native tree."""
from pathlib import Path
import hashlib, subprocess, sys

PIN="886fc079838ca7400cb2b42b36e2a65aa1daabe8"

def replace_one(files: dict[Path,str], path: Path, old: str, new: str):
    value=files[path]
    if value.count(old)!=1: raise ValueError(f"LitePerf anchor drift: {path.name}: {old[:50]!r}")
    files[path]=value.replace(old,new)

def stage(root: Path):
    root=root.resolve(strict=True)
    ref="HEAD" if subprocess.run(["git","-C",root,"rev-parse","--verify","HEAD"],capture_output=True).returncode==0 else "FETCH_HEAD"
    revision=subprocess.run(["git","-C",root,"rev-parse",ref],capture_output=True,text=True,check=True).stdout.strip()
    if revision!=PIN: raise ValueError("Pinned native revision mismatch")
    header=Path(__file__).with_name("lite-native-perf.h").read_bytes()
    target=root/"src/lite-native-perf.h"
    if target.exists(): raise ValueError("LitePerf already staged")
    tts=root/"src/pipeline-tts.cpp"; codec=root/"src/pipeline-codec.cpp"; mask=root/"src/maskgit-tts.h"; omni=root/"src/omnivoice.cpp"
    files={path:path.read_text(encoding="utf-8") for path in (tts,codec,mask,omni)}
    replace_one(files,tts,'#include "pipeline-tts.h"','#include "pipeline-tts.h"\n#include "lite-native-perf.h"')
    replace_one(files,codec,'#include "pipeline-codec.h"','#include "pipeline-codec.h"\n#include "lite-native-perf.h"')
    replace_one(files,mask,'#include "debug.h"','#include "debug.h"\n#include "lite-native-perf.h"')
    replace_one(files,omni,'#include "omnivoice.h"','#include "omnivoice.h"\n#include "lite-native-perf.h"')
    replace_one(files,omni,'struct ov_context * ov_init(const struct ov_init_params * params) {','struct ov_context * ov_init(const struct ov_init_params * params) {\n    LitePerfScope lite_model_load_scope(LitePerfBucket::ModelLoad);')
    replace_one(files,tts,'    RefEncoded r           = {};','    LitePerfScope lite_perf_scope(LitePerfBucket::Reference);\n    RefEncoded r           = {};')
    replace_one(files,tts,'    if (!params) {','    LitePerfScope lite_perf_scope(LitePerfBucket::Synth);\n    if (!params) {')
    replace_one(files,tts,'    if (T <= 0) {','    LitePerfScope lite_prompt_scope(LitePerfBucket::PromptDuration);\n    if (T <= 0) {')
    replace_one(files,tts,'    size_t before = audio.size();','    LitePerfScope lite_postproc_scope(LitePerfBucket::Postprocess);\n    size_t before = audio.size();')
    replace_one(files,mask,'    const int K       = prompt->K;','    LitePerfScope lite_perf_scope(LitePerfBucket::Maskgit);\n    const int K       = prompt->K;')
    replace_one(files,codec,'std::vector<float> pipeline_codec_decode(PipelineCodec * pc, const int32_t * codes, int num_codebooks, int n_frames) {','std::vector<float> pipeline_codec_decode(PipelineCodec * pc, const int32_t * codes, int num_codebooks, int n_frames) {\n    LitePerfScope lite_perf_scope(LitePerfBucket::CodecDecode);')
    encode='''std::vector<int32_t> pipeline_codec_encode(PipelineCodec * pc,
                                           const float *   audio_24k,
                                           int             n_samples,
                                           const char *    dump_dir) {'''
    replace_one(files,codec,encode,encode+'\n    LitePerfScope lite_perf_scope(LitePerfBucket::CodecEncode);')
    noisy='''        fprintf(stderr, "[MaskGIT-Step] %d/%d demask=%d remaining=%d\\n", step + 1, cfg.num_step, k_demask,
                (int) std::count(tokens.begin(), tokens.end(), mask_id));'''
    replace_one(files,mask,noisy,'        // LitePerf/v1: per-step logs intentionally suppressed; aggregate remains below.')
    replace_one(files,mask,'    return tokens;\n}', '    lite_perf_state().maskgit_lm_forward_us += (uint64_t) (fwd_total_ms * 1000.0);\n    lite_perf_state().chunk_count++;\n    return tokens;\n}')
    target.write_bytes(header)
    for path,value in files.items(): path.write_text(value,encoding="utf-8")
    print("LITE_NATIVE_PERF_STAGED",PIN,hashlib.sha256(header).hexdigest())

if __name__=="__main__":
    if len(sys.argv)!=2: raise SystemExit("usage: instrument-native-performance.py <pinned-source-root>")
    stage(Path(sys.argv[1]))
