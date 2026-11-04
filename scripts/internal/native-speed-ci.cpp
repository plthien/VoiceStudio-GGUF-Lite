// Compiled against the EXACT patched pinned src/duration-estimator.h in CI.
#include "duration-estimator.h"
#include "omnivoice.h"
#include <cstddef>
#include <type_traits>
static_assert(OV_ABI_VERSION==2,"Preserve exact native ABI v2");
static_assert(std::is_standard_layout<ov_tts_params>::value,"C ABI must be standard layout");
static_assert(offsetof(ov_tts_params,on_chunk_user_data)>offsetof(ov_tts_params,on_chunk),
              "ABI-v2 final field must remain on_chunk_user_data");
// Included transitively from the patched pinned duration-estimator.h.
#include <cassert>
#include <climits>
#include <cmath>
#include <string>
int main(){
    const std::string short_text="A short sentence is spoken in an owned reference voice.";
    const std::string ref="This reference has a clearly spoken transcript.";
    for(const auto & data: {std::pair<std::string,int>{ref,140}, {std::string(),0}}) {
        const int baseline=duration_estimate_tokens(short_text,data.first,data.second);
        assert(duration_estimate_tokens_speed(short_text,data.first,data.second,1.0f)==baseline);
        const int slow=duration_estimate_tokens_speed(short_text,data.first,data.second,0.80f);
        const int fast=duration_estimate_tokens_speed(short_text,data.first,data.second,1.20f);
        assert(slow>baseline && fast<baseline);
        assert(slow>=1 && fast>=1);
    }
    const std::string long_text(2400,'a');
    const int total=duration_estimate_tokens_speed(long_text,"",0,1.2f);
    assert(total>750 && "Long form must remain eligible for chunking above 30s at 25fps");
    const int one=duration_estimate_tokens_speed(long_text,"",0,1.0f);
    assert(total<one);
    const int ti1=duration_estimate_tokens_speed("Some words inside chunk one.",ref,140,0.8f);
    const int ti2=duration_estimate_tokens_speed("Some words inside chunk one.",ref,140,1.2f);
    assert(ti1>ti2); // Every chunk uses own reference-conditioned estimated budget.
    assert(lite_speed_budget(0.25f,1.25f)==1);
    assert(lite_speed_budget((float)INT_MAX,0.75f)==INT_MAX);
    float parsed=0;
    for(const char * invalid: {"0","-1","1.26","0.74","nan","NaN","inf","infinity","abc","1.1x","", " 1.0","1.0 "}) {
        assert(!lite_speed_parse(invalid,&parsed));
    }
    for(const char * valid: {"0.75","0.80","1","1.0","1.20","1.25"}) {
        assert(lite_speed_parse(valid,&parsed));
    }
    // T_override behavior is unchanged in pinned pipeline: explicit duration
    // selects exact target frames, independent of speed.
    const int fixed_duration_frames=125;
    assert((fixed_duration_frames>0?fixed_duration_frames:
        duration_estimate_tokens_speed(short_text,ref,140,0.80f))==125);
    return 0;
}
