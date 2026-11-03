#include "lite-native-perf.h"
#include "lite-reference-cache.h"
#include "lite-maskgit-experiments.h"
#include <cassert>
#include <cstdint>
#include <cstring>
#include <limits>
#include <vector>

struct Slot { std::vector<float> pcm; const char *text=nullptr; bool valid=false; };
static std::vector<int16_t> fixture(Slot &slot,const std::vector<float>&pcm,const char*text,bool enabled,bool &hit){
    hit=enabled&&slot.valid&&std::strcmp(slot.text,text)==0&&lite_pcm_equal(slot.pcm,pcm.data(),(int)pcm.size());
    if(!hit&&enabled){slot.pcm=pcm;slot.text=text;slot.valid=true;}
    std::vector<int16_t> out;for(float v:pcm)out.push_back((int16_t)(v*1000));return out;
}
int main(){
    std::vector<float>a={.01f,-.02f,.03f},b={.04f,-.05f};Slot slot;bool hit=false;
    auto off=fixture(slot,a,"same",false,hit);assert(!hit);
    auto miss=fixture(slot,a,"same",true,hit);assert(!hit&&off==miss);
    auto warm=fixture(slot,a,"same",true,hit);assert(hit&&warm==off);
    fixture(slot,a,"different",true,hit);assert(!hit);
    fixture(slot,b,"same",true,hit);assert(!hit);
    fixture(slot,a,"same",true,hit);assert(!hit); // A->B->A is a miss.
    float nan=std::numeric_limits<float>::quiet_NaN();assert(!lite_pcm_valid(&nan,1));
    lite_perf_reset(32,true);{LitePerfScope scope(LitePerfBucket::Reference);}
    assert(lite_perf_state().maskgit_steps==32&&lite_perf_state().flash_attention==1);
    return 0;
}
