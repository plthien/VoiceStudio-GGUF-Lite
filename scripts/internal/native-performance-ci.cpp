#include "lite-native-perf.h"
#include "lite-reference-cache.h"
#include "lite-maskgit-experiments.h"
#include <cassert>
#include <cstdint>
#include <cstring>
#include <limits>
#include <string>
#include <vector>

struct Prepared {
    std::vector<int32_t> ref_codes;
    int ref_T=0;
    std::string ref_text;
    float ref_rms_for_postproc=-1;
};
struct ContextSlot {
    bool valid=false, preprocess=false;
    std::vector<float> source_pcm;
    std::string input_text;
    Prepared prepared={};
};
struct MockCodec {
    int encodes=0;
    Prepared encode(const std::vector<float>&a, const std::string&txt) {
        encodes++;
        // This stub models only the encode boundary. It is NOT an acoustic
        // parity test for the real pinned model, which is owner-only.
        return {{1,2,3},int(a.size()),txt,.0003f};
    }
};
int main(){
    std::vector<float>a={.0001f,-.0002f,.0003f};
    std::vector<float>b={.04f,-.05f};
    ContextSlot slot;MockCodec codec;
    auto run=[&](const std::vector<float>&pcm,const std::string&text,bool preprocess,bool enabled) {
        return lite_prepare_or_reuse_ref(slot,enabled,pcm.data(),int(pcm.size()),
            text,preprocess,[&]{return codec.encode(pcm,text);});
    };
    lite_perf_reset(32,true);
    const auto off=run(a,"original",false,false);
    assert(codec.encodes==1 && off.ref_rms_for_postproc==.0003f);
    const auto first=run(a,"original",false,true);
    assert(codec.encodes==2 && first.ref_codes==off.ref_codes);
    assert(lite_perf_state().cache_misses==1);
    const auto hit=run(a,"original",false,true);
    assert(codec.encodes==2 && hit.ref_rms_for_postproc==.0003f);
    assert(hit.ref_text=="original" && lite_perf_state().cache_hits==1);
    run(a,"different transcript",false,true);
    assert(codec.encodes==3);
    run(a,"different transcript",true,true);
    assert(codec.encodes==4);
    run(b,"original",false,true);
    assert(codec.encodes==5);
    run(a,"original",false,true);
    assert(codec.encodes==6); // A -> B -> A miss; single slot.
    a[0]=.000101f;
    run(a,"original",false,true);
    assert(codec.encodes==7); // Exact float32 byte comparison.
    a[0]=.0001f;
    run(a,"original",false,true);
    assert(codec.encodes==8);
    run(a,"original",false,true);
    assert(codec.encodes==8);
    // Speed, seed and pause are never cache keys because generated output
    // is not cached. A mock postprocess remains dependent on each control.
    auto synth=[&](uint32_t seed,float speed,int pauses){
        const auto &p=run(a,"original",false,true);
        return int(seed)+int(speed*100)+pauses+int(p.ref_codes.size());
    };
    assert(synth(42,1.f,0)!=synth(43,1.f,0));
    assert(synth(42,.85f,0)!=synth(42,1.f,0));
    assert(synth(42,1.f,2)!=synth(42,1.f,10));
    assert(codec.encodes==8);
    // Context unload/restart destroys the one cache slot.
    slot=ContextSlot{};
    run(a,"original",false,true);
    assert(codec.encodes==9);
    float nan=std::numeric_limits<float>::quiet_NaN();
    std::vector<float> invalid={nan};
    const auto rejected=run(invalid,"invalid",false,true);
    assert(rejected.ref_codes.empty() && !slot.valid && codec.encodes==9);
    // Oversized reference rejected before any encoder call.
    std::vector<float>oversized(60*24000+1,.001f);
    run(oversized,"long",false,true);
    assert(codec.encodes==9 && !slot.valid);
    assert(lite_pcm_equal(a,a.data(),int(a.size())));
    lite_perf_reset(32,true);
    {LitePerfScope scope(LitePerfBucket::Reference);}
    assert(lite_perf_state().maskgit_steps==32 && lite_perf_state().flash_attention==1);
    return 0;
}
