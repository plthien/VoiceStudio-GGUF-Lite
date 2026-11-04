#pragma once
// LitePerf/v1 internal-only profiler. No text, paths, voice IDs or audio.
#include <chrono>
#include <cstdint>

enum class LitePerfBucket { ModelLoad, Synth, Reference, PromptDuration, Maskgit, CodecEncode, CodecDecode, Postprocess };
struct LitePerfState {
    uint64_t model_load_us=0, synth_us=0, reference_us=0, prompt_duration_us=0, maskgit_us=0;
    uint64_t maskgit_lm_forward_us=0, codec_encode_us=0, codec_decode_us=0, postprocess_us=0;
    uint64_t reference_codec_encode_calls=0, cache_hits=0, cache_misses=0;
    uint64_t encoded_ref_samples=0, encoded_ref_bytes=0, chunk_count=0;
    uint32_t maskgit_steps=32, flash_attention=0;
    uint32_t ref_cache_hit=0;
};
inline LitePerfState & lite_perf_state(){ static thread_local LitePerfState value; return value; }
inline void lite_perf_reset(uint32_t steps, bool fa){
    uint64_t load=lite_perf_state().model_load_us, hits=lite_perf_state().cache_hits, misses=lite_perf_state().cache_misses;
    lite_perf_state()={}; lite_perf_state().model_load_us=load;
    lite_perf_state().cache_hits=hits; lite_perf_state().cache_misses=misses;
    lite_perf_state().maskgit_steps=steps; lite_perf_state().flash_attention=fa?1U:0U;
}
struct LitePerfScope {
    LitePerfBucket bucket; std::chrono::steady_clock::time_point start=std::chrono::steady_clock::now();
    explicit LitePerfScope(LitePerfBucket b):bucket(b){}
    ~LitePerfScope(){
        uint64_t us=(uint64_t)std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::steady_clock::now()-start).count();
        auto &p=lite_perf_state(); switch(bucket){
        case LitePerfBucket::ModelLoad:p.model_load_us+=us;break; case LitePerfBucket::Synth:p.synth_us+=us;break;
        case LitePerfBucket::Reference:p.reference_us+=us;break; case LitePerfBucket::PromptDuration:p.prompt_duration_us+=us;break;
        case LitePerfBucket::Maskgit:p.maskgit_us+=us;break; case LitePerfBucket::CodecEncode:p.codec_encode_us+=us;break;
        case LitePerfBucket::CodecDecode:p.codec_decode_us+=us;break; case LitePerfBucket::Postprocess:p.postprocess_us+=us;break; }
    }
};
