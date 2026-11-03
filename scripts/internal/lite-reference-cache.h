#pragma once
// Exact single-slot reference-conditioning cache helpers.
#include <cstdlib>
#include <cstring>
#include <cmath>
#include <stdexcept>
#include <vector>
#include <string>
#include <utility>

inline bool lite_ref_cache_setting(){
    const char *value=std::getenv("GGUF_LITE_EXPERIMENT_REF_CACHE");
    if(!value||std::strcmp(value,"0")==0)return false;
    if(std::strcmp(value,"1")==0)return true;
    throw std::runtime_error("GGUF_LITE_EXPERIMENT_REF_CACHE must be 0 or 1");
}
inline bool lite_pcm_equal(const std::vector<float>&a,const float*b,int n){
    return n>=0&&a.size()==(size_t)n&&(n==0||std::memcmp(a.data(),b,(size_t)n*sizeof(float))==0);
}
inline bool lite_pcm_valid(const float*p,int n){
    if(!p||n<=0)return false; for(int i=0;i<n;i++)if(!std::isfinite(p[i]))return false; return true;
}

 
// This is the *production* reference-conditioning decision path. The staged
// pinned pipeline wrapper calls this same compiled template; the CPU no-model
// native fixture supplies only a stub codec encoder and observes its calls.
// Storage is a single context-owned slot. Never cache seed, speed, output PCM,
// pause arrangement or synthesized audio.
template<class Slot, class Encoder>
inline const auto& lite_prepare_or_reuse_ref(
        Slot& slot, bool enabled, const float* pcm, int samples,
        const std::string& text, bool preprocess, Encoder&& encode) {
    if (samples < 0 || samples > 60*24000 ||
        (samples > 0 && !lite_pcm_valid(pcm, samples))) {
        slot.prepared={};
        slot.valid=false;
        slot.source_pcm.clear();
        slot.input_text.clear();
        return slot.prepared;
    }
    if (enabled && slot.valid && slot.preprocess==preprocess &&
        slot.input_text==text &&
        lite_pcm_equal(slot.source_pcm, pcm, samples)) {
        lite_perf_state().cache_hits++;
        lite_perf_state().ref_cache_hit=1;
        return slot.prepared;
    }
    if (enabled && pcm && samples>0) lite_perf_state().cache_misses++;
    slot.prepared=std::forward<Encoder>(encode)();
    slot.valid=false;
    if (enabled && pcm && samples>0 && !slot.prepared.ref_codes.empty()) {
        slot.source_pcm.assign(pcm, pcm+samples);
        slot.input_text=text;
        slot.preprocess=preprocess;
        slot.valid=true;
    } else {
        slot.source_pcm.clear();
        slot.input_text.clear();
    }
    return slot.prepared;
}
