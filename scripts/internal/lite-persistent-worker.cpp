// LitePersistent/v1: bounded, binary-safe private stdio worker.
// Stdout is protocol-only. Text, reference bytes and paths are never logged.
#include "omnivoice.h"
#include "../src/lite-maskgit-experiments.h"
#include "../src/lite-native-perf.h"
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <limits>
#include <string>
#include <vector>
#if defined(_WIN32)
# include <fcntl.h>
# include <io.h>
#endif

namespace {
constexpr uint32_t MAGIC=0x3150544cU, MAX_CONTROL=64U*1024U, MAX_AUDIO=100U*1024U*1024U;
constexpr uint16_t VERSION=1, HELLO=1, LOAD=2, SYNTH=3, UNLOAD=4, SHUTDOWN=5, GET_PERF=6;
constexpr uint16_t CAPABILITY=101, READY=102, RESULT=103, OK=104, PERF=105, ERROR=255;
struct Header { uint16_t command=0; uint32_t sequence=0; uint32_t length=0; };
static uint16_t u16(const uint8_t*p){return uint16_t(p[0])|uint16_t(p[1])<<8;}
static uint32_t u32(const uint8_t*p){return uint32_t(p[0])|uint32_t(p[1])<<8|uint32_t(p[2])<<16|uint32_t(p[3])<<24;}
static void put16(std::vector<uint8_t>&v,uint16_t n){v.push_back(uint8_t(n));v.push_back(uint8_t(n>>8));}
static void put32(std::vector<uint8_t>&v,uint32_t n){for(int i=0;i<4;i++)v.push_back(uint8_t(n>>(8*i)));}
static void put64(std::vector<uint8_t>&v,uint64_t n){for(int i=0;i<8;i++)v.push_back(uint8_t(n>>(8*i)));}
static bool exact(void*p,size_t n){return n==0||fread(p,1,n,stdin)==n;}
static bool read_header(Header&h){
    uint8_t b[16]; if(!exact(b,sizeof(b))) return false;
    if(u32(b)!=MAGIC||u16(b+4)!=VERSION) return false;
    h.command=u16(b+6);h.sequence=u32(b+8);h.length=u32(b+12);return true;
}
static bool write_frame(uint16_t cmd,uint32_t seq,const std::vector<uint8_t>&body={}){
    std::vector<uint8_t> h;h.reserve(16);put32(h,MAGIC);put16(h,VERSION);put16(h,cmd);put32(h,seq);put32(h,uint32_t(body.size()));
    return fwrite(h.data(),1,h.size(),stdout)==h.size() &&
           (body.empty()||fwrite(body.data(),1,body.size(),stdout)==body.size()) && fflush(stdout)==0;
}
static bool field(const std::vector<uint8_t>&p,size_t&at,std::vector<uint8_t>&out,uint32_t limit){
    if(at+4>p.size())return false;uint32_t n=u32(p.data()+at);at+=4;
    if(n>limit||at+n>p.size())return false;out.assign(p.begin()+at,p.begin()+at+n);at+=n;return true;
}
static bool text_field(const std::vector<uint8_t>&p,size_t&at,std::string&out,uint32_t limit){
    std::vector<uint8_t> v;if(!field(p,at,v,limit)||std::find(v.begin(),v.end(),0)!=v.end())return false;
    out.assign(v.begin(),v.end());return true;
}
static int run(){
#if defined(_WIN32)
    _setmode(_fileno(stdin),_O_BINARY);_setmode(_fileno(stdout),_O_BINARY);
#endif
    int maskgit_steps=32;bool flash_attention=true,profile_enabled=false;
    try {
        maskgit_steps=lite_maskgit_steps();flash_attention=lite_flash_attention();
        const char*perf=std::getenv("GGUF_LITE_PERF");
        if(perf&&std::strcmp(perf,"0")!=0&&std::strcmp(perf,"1")!=0)return 2;
        profile_enabled=perf&&std::strcmp(perf,"1")==0;
        const char*cache=std::getenv("GGUF_LITE_EXPERIMENT_REF_CACHE");
        if(cache&&std::strcmp(cache,"0")!=0&&std::strcmp(cache,"1")!=0)return 2;
    } catch(const std::exception&){return 2;}
    ov_context*ctx=nullptr;uint32_t init_count=0,synth_count=0;bool ever_loaded=false;
    for(;;){
        Header h;if(!read_header(h))break;
        uint32_t bound=h.command==SYNTH?MAX_AUDIO:MAX_CONTROL;
        if(h.length>bound){write_frame(ERROR,h.sequence);break;}
        std::vector<uint8_t> p(h.length);if(!exact(p.data(),p.size()))break;
        if(h.command==HELLO){
            const char marker[]="LitePersistent/v1";std::vector<uint8_t>b(marker,marker+sizeof(marker)-1);
            if(!p.empty()||!write_frame(CAPABILITY,h.sequence,b))break;
        }else if(h.command==LOAD){
            size_t at=0;std::string model,codec;
            if(ctx||ever_loaded||!text_field(p,at,model,32768)||!text_field(p,at,codec,32768)||at!=p.size()){
                if(!write_frame(ERROR,h.sequence))break;continue;
            }
            ov_init_params ip;ov_init_default_params(&ip);ip.model_path=model.c_str();ip.codec_path=codec.c_str();ip.use_fa=flash_attention;
            ctx=ov_init(&ip);if(!ctx){if(!write_frame(ERROR,h.sequence))break;continue;}
            ever_loaded=true;++init_count;std::vector<uint8_t>b;put64(b,uint64_t(reinterpret_cast<uintptr_t>(ctx)));put32(b,init_count);put32(b,synth_count);
            if(!write_frame(READY,h.sequence,b))break;
        }else if(h.command==SYNTH){
            if(!ctx||p.size()<8){if(!write_frame(ERROR,h.sequence))break;continue;}
            uint32_t seed=u32(p.data());float speed=0;uint32_t speed_bits=u32(p.data()+4);std::memcpy(&speed,&speed_bits,4);
            size_t at=8;std::string text,lang,ref_text;std::vector<uint8_t>pcm;
            if(!std::isfinite(speed)||speed<.75f||speed>1.25f||
               !text_field(p,at,text,4096)||text.empty()||!text_field(p,at,lang,64)||
               !text_field(p,at,ref_text,4096)||ref_text.empty()||
               !field(p,at,pcm,60U*24000U*2U)||at!=p.size()||pcm.empty()||(pcm.size()&1U)){
                if(!write_frame(ERROR,h.sequence))break;continue;
            }
            std::vector<float> ref(pcm.size()/2);
            for(size_t i=0;i<ref.size();i++){uint16_t bits=uint16_t(pcm[i*2])|uint16_t(pcm[i*2+1])<<8;int32_t s=bits<32768?int32_t(bits):int32_t(bits)-65536;ref[i]=float(s)/32768.f;}
            ov_tts_params tp;ov_tts_default_params(&tp);tp.text=text.c_str();tp.lang=lang.c_str();tp.ref_audio_24k=ref.data();tp.ref_n_samples=int(ref.size());tp.ref_text=ref_text.c_str();tp.mg_num_step=maskgit_steps;
            if(profile_enabled)lite_perf_reset(uint32_t(maskgit_steps),flash_attention);
            tp.mg_seed=seed==0xffffffffU?uint64_t(-1):uint64_t(seed);ov_audio audio={};
            auto status=ov_synthesize_with_speed(ctx,&tp,speed,&audio);
            if(status!=OV_STATUS_OK||!audio.samples||audio.sample_rate!=24000||audio.channels!=1||audio.n_samples<=0||uint64_t(audio.n_samples)*2U>MAX_AUDIO-24U){
                ov_audio_free(&audio);if(!write_frame(ERROR,h.sequence))break;continue;
            }
            ++synth_count;std::vector<uint8_t>b;b.reserve(24+size_t(audio.n_samples)*2);put64(b,uint64_t(reinterpret_cast<uintptr_t>(ctx)));put32(b,init_count);put32(b,synth_count);put32(b,24000);put32(b,uint32_t(audio.n_samples));
            for(int i=0;i<audio.n_samples;i++){float x=std::isfinite(audio.samples[i])?std::max(-1.f,std::min(1.f,audio.samples[i])):0.f;int32_t q=x<=-1.f?-32768:int32_t(std::lrint(x*32767.f));uint16_t u=uint16_t(int16_t(q));b.push_back(uint8_t(u));b.push_back(uint8_t(u>>8));}
            ov_audio_free(&audio);if(!write_frame(RESULT,h.sequence,b))break;
        }else if(h.command==GET_PERF){
            if(!profile_enabled||!p.empty()){if(!write_frame(ERROR,h.sequence))break;continue;}
            const auto&m=lite_perf_state();std::vector<uint8_t>b;const char marker[]="LitePerf/v1";
            uint64_t ref_pre=m.reference_us>m.codec_encode_us?m.reference_us-m.codec_encode_us:0;
            uint64_t prompt=m.prompt_duration_us>m.maskgit_us?m.prompt_duration_us-m.maskgit_us:0;
            uint64_t sampling=m.maskgit_us>m.maskgit_lm_forward_us?m.maskgit_us-m.maskgit_lm_forward_us:0;
            uint64_t accounted=ref_pre+m.codec_encode_us+prompt+m.maskgit_lm_forward_us+sampling+m.codec_decode_us+m.postprocess_us;
            uint64_t other=m.synth_us>accounted?m.synth_us-accounted:0;
            b.insert(b.end(),marker,marker+sizeof(marker)-1);put32(b,1);put32(b,152);put32(b,0);
            put64(b,m.model_load_us);put64(b,ref_pre);put64(b,m.codec_encode_us);put64(b,prompt);
            put64(b,m.maskgit_lm_forward_us);put64(b,sampling);put64(b,m.codec_decode_us);put64(b,m.postprocess_us);
            put64(b,m.synth_us);put64(b,other);put64(b,m.reference_codec_encode_calls);put64(b,m.cache_hits);
            put64(b,m.cache_misses);put64(b,m.encoded_ref_samples);put64(b,m.encoded_ref_bytes);put64(b,m.chunk_count);
            put32(b,m.maskgit_steps);put32(b,m.flash_attention);put32(b,m.ref_cache_hit);if(!write_frame(PERF,h.sequence,b))break;
        }else if(h.command==UNLOAD){
            if(!p.empty()){if(!write_frame(ERROR,h.sequence))break;continue;}
            if(ctx){ov_free(ctx);ctx=nullptr;}if(!write_frame(OK,h.sequence))break;
        }else if(h.command==SHUTDOWN){
            if(ctx){ov_free(ctx);ctx=nullptr;}write_frame(OK,h.sequence);return 0;
        }else{if(!write_frame(ERROR,h.sequence))break;}
    }
    if(ctx)ov_free(ctx);return 2;
}
}
int lite_persistent_worker_main(){return run();}
