#pragma once
#include <cstdlib>
#include <cstring>
#include <stdexcept>
#include <string>
#include <initializer_list>
inline int lite_maskgit_steps(){
    const char *v=std::getenv("GGUF_LITE_EXPERIMENT_MASKGIT_STEPS"); if(!v||!*v)return 32;
    for(int n: {32,24,16,12,8}) if(std::strcmp(v,std::to_string(n).c_str())==0)return n;
    throw std::runtime_error("GGUF_LITE_EXPERIMENT_MASKGIT_STEPS invalid");
}
inline bool lite_flash_attention(){
    const char *v=std::getenv("GGUF_LITE_EXPERIMENT_FA");
    if(!v||!*v||std::strcmp(v,"baseline")==0||std::strcmp(v,"1")==0)return true;
    if(std::strcmp(v,"0")==0)return false;
    throw std::runtime_error("GGUF_LITE_EXPERIMENT_FA must be baseline, 0 or 1");
}
