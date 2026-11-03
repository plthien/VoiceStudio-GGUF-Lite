#pragma once
// Exact single-slot reference-conditioning cache helpers.
#include <cstdlib>
#include <cstring>
#include <cmath>
#include <stdexcept>
#include <vector>

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
