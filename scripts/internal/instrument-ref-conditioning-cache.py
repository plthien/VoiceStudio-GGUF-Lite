"""Exact, single-slot, opt-in reference cache patch for the pinned source."""
from pathlib import Path
import hashlib,subprocess,sys
PIN="886fc079838ca7400cb2b42b36e2a65aa1daabe8"

def one(files,path,old,new):
    value=files[path]
    if value.count(old)!=1: raise ValueError(f"reference-cache anchor drift: {path.name}")
    files[path]=value.replace(old,new)

def stage(root:Path):
    root=root.resolve(strict=True)
    ref="HEAD" if subprocess.run(["git","-C",root,"rev-parse","--verify","HEAD"],capture_output=True).returncode==0 else "FETCH_HEAD"
    if subprocess.run(["git","-C",root,"rev-parse",ref],capture_output=True,text=True,check=True).stdout.strip()!=PIN:
        raise ValueError("Pinned native revision mismatch")
    helper=Path(__file__).with_name("lite-reference-cache.h").read_bytes(); target=root/"src/lite-reference-cache.h"
    if target.exists(): raise ValueError("reference cache already staged")
    h=root/"src/pipeline-tts.h"; cpp=root/"src/pipeline-tts.cpp"
    files={path:path.read_text(encoding="utf-8") for path in (h,cpp)}
    one(files,h,'struct PipelineTTS {','''struct RefEncoded {
    bool has_ref;
    std::vector<int32_t> ref_codes;
    int ref_T;
    std::string ref_text;
    float ref_rms_for_postproc;
};
struct LiteRefCacheSlot {
    bool valid=false, preprocess=false;
    std::vector<float> source_pcm;
    std::string input_text;
    RefEncoded prepared={};
};

struct PipelineTTS {''')
    one(files,h,'    bool clamp_fp16;','''    bool clamp_fp16;
    bool lite_ref_cache_enabled;
    LiteRefCacheSlot lite_ref_cache;''')
    one(files,cpp,'#include "pipeline-tts.h"','#include "pipeline-tts.h"\n#include "lite-reference-cache.h"')
    one(files,cpp,'    pt->clamp_fp16     = clamp_fp16;','''    pt->clamp_fp16     = clamp_fp16;
    pt->lite_ref_cache_enabled = lite_ref_cache_setting();
    pt->lite_ref_cache = {};''')
    ref_struct='''struct RefEncoded {
    bool                 has_ref;
    std::vector<int32_t> ref_codes;
    int                  ref_T;
    std::string          ref_text;
    float                ref_rms_for_postproc;
};

'''
    one(files,cpp,ref_struct,'')
    one(files,cpp,'    r.ref_codes = pipeline_codec_encode(pc, ref_audio.data(), n_aligned, dump_dir);','''    lite_perf_state().reference_codec_encode_calls++;
    lite_perf_state().encoded_ref_samples=(uint64_t)n_aligned;
    lite_perf_state().encoded_ref_bytes=(uint64_t)n_aligned*sizeof(float);
    r.ref_codes = pipeline_codec_encode(pc, ref_audio.data(), n_aligned, dump_dir);''')
    one(files,cpp,'    return r;\n}\n\nov_status pipeline_tts_synthesize','''    return r;
}

static const RefEncoded & prepare_or_reuse_ref(PipelineTTS *pt, PipelineCodec *pc,
        const float *pcm, int samples, const std::string &text, bool preprocess, const char *dump_dir) {
    return lite_prepare_or_reuse_ref(
        pt->lite_ref_cache, pt->lite_ref_cache_enabled, pcm, samples, text, preprocess,
        [&]() { return tts_encode_ref(pt, pc, pcm, samples, text, preprocess, dump_dir); });
}

ov_status pipeline_tts_synthesize''')
    one(files,cpp,'    RefEncoded re =\n        tts_encode_ref(pt, pc, has_raw ? params->ref_audio_24k : nullptr, has_raw ? params->ref_n_samples : 0, ref_text,\n                       params->preprocess_prompt, params->dump_dir);','''    const RefEncoded & re =
        prepare_or_reuse_ref(pt, pc, has_raw ? params->ref_audio_24k : nullptr, has_raw ? params->ref_n_samples : 0,
                             ref_text, params->preprocess_prompt, params->dump_dir);''')
    target.write_bytes(helper)
    for path,value in files.items():path.write_text(value,encoding="utf-8")
    print("LITE_REFERENCE_CACHE_STAGED",PIN,hashlib.sha256(helper).hexdigest())
if __name__=="__main__":
    if len(sys.argv)!=2:raise SystemExit("usage: instrument-ref-conditioning-cache.py <pinned-source-root>")
    stage(Path(sys.argv[1]))
