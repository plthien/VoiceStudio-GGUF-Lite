// Opt-in feasibility ONLY. Not a resident service and never linked into the Lite API.
// Run on owner's native Vulkan0 device with two private, owned reference PCM files.
// Input PCM: signed little-endian mono 24000Hz, generated separately by ffmpeg.
// This verifies a *real* ov_context survives A -> B -> A, with fresh params,
// fresh ref buffers and explicit ov_audio_free after every call. No private
// reference bytes, transcript text or generated audio are printed or stored.
#include "omnivoice.h"
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iostream>
#include <iterator>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

static std::vector<char> read_bounded(const char * name, size_t max_bytes) {
    std::ifstream f(name, std::ios::binary | std::ios::ate);
    if (!f) throw std::runtime_error("private input unavailable");
    const auto length=f.tellg();
    if (length<=0 || static_cast<uint64_t>(length)>max_bytes)
        throw std::runtime_error("private input exceeds bound or is empty");
    f.seekg(0);
    std::vector<char> bytes(static_cast<size_t>(length));
    if (!f.read(bytes.data(),length)) throw std::runtime_error("private input truncated");
    return bytes;
}
static std::vector<float> read_pcm(const char* name) {
    // At most 60s @ 24k/mono/PCM16. Enforce even sample bytes.
    auto bytes=read_bounded(name,60u*24000u*2u);
    if ((bytes.size()&1u)!=0u || bytes.size()<480u)
        throw std::runtime_error("reference must be 24kHz mono s16le PCM");
    std::vector<float> pcm(bytes.size()/2);
    for (size_t i=0;i<pcm.size();++i) {
        const uint16_t bits=static_cast<uint8_t>(bytes[i*2]) |
                            (static_cast<uint16_t>(static_cast<uint8_t>(bytes[i*2+1]))<<8);
        const int32_t signed_sample = bits < 32768u ? static_cast<int32_t>(bits)
                                                      : static_cast<int32_t>(bits)-65536;
        pcm[i] = static_cast<float>(signed_sample)/32768.f;
    }
    return pcm;
}
static std::string read_transcript(const char* name) {
    auto bytes=read_bounded(name,4096);
    std::string result(bytes.begin(),bytes.end());
    if (result.find('\0')!=std::string::npos)
        throw std::runtime_error("reference transcript contains NUL");
    while (!result.empty() && (result.back()=='\r'||result.back()=='\n'))
        result.pop_back();
    if (result.empty()) throw std::runtime_error("reference transcript empty");
    return result;
}
struct AudioFingerprint {
    std::vector<uint8_t> canonical_pcm;
    int sample_rate=0;
    int n_samples=0;
};
static AudioFingerprint synth(ov_context* ctx,const std::vector<float>& reference,
                              const std::string& ref_text,const std::string& text,
                              uint64_t seed,float speed) {
    ov_tts_params params;
    ov_tts_default_params(&params); // fresh request-scoped params; no stale seed/ref
    params.text=text.c_str();
    params.lang="en";
    params.ref_audio_24k=reference.data();
    params.ref_n_samples=static_cast<int>(reference.size());
    params.ref_text=ref_text.c_str();
    params.mg_seed=seed;
    ov_audio out={};
    const auto status=ov_synthesize_with_speed(ctx,&params,speed,&out);
    if (status!=OV_STATUS_OK) {
        ov_audio_free(&out);
        throw std::runtime_error("synthesis returned non-OK status");
    }
    if (!out.samples || out.sample_rate!=24000 || out.channels!=1 ||
        out.n_samples<=0 || out.n_samples>24000*600) {
        ov_audio_free(&out);
        throw std::runtime_error("native output violates PCM bounds");
    }
    AudioFingerprint result;
    result.sample_rate=out.sample_rate;
    result.n_samples=out.n_samples;
    result.canonical_pcm.reserve(static_cast<size_t>(out.n_samples)*2u);
    // Mirror 16-bit WAV quantisation to compare two outputs without storing audio.
    for (int i=0;i<out.n_samples;i++) {
        const float sample=std::isfinite(out.samples[i]) ? out.samples[i] : 0.f;
        const float bounded=std::max(-1.f,std::min(1.f,sample));
        const int32_t value=bounded<=-1.f ? -32768 :
                            static_cast<int32_t>(std::lrint(bounded*32767.f));
        const auto u=static_cast<uint16_t>(static_cast<int16_t>(value));
        result.canonical_pcm.push_back(static_cast<uint8_t>(u & 0xffu));
        result.canonical_pcm.push_back(static_cast<uint8_t>(u >> 8));
    }
    ov_audio_free(&out);
    return result;
}
int main(int argc,char**argv) {
    // Paths remain private on owner's computer; probe logs no filenames.
    if(argc!=9) {
        std::cerr<<"usage: lite-reuse-probe <base.gguf> <tokenizer.gguf> <A.s16le> <A.txt> <B.s16le> <B.txt> <seed-0..2147483647> <speed-0.75..1.25>\n";
        return 2;
    }
    try {
        const std::string seed_text=argv[7],speed_text=argv[8];
        size_t pos=0;const auto seed=std::stoull(seed_text,&pos,10);
        if (pos!=seed_text.size()||seed>2147483647ULL) throw std::runtime_error("bad seed");
        pos=0; const float speed=std::stof(speed_text,&pos);
        if(pos!=speed_text.size()||!std::isfinite(speed)||speed<.75f||speed>1.25f)
            throw std::runtime_error("bad speed");
        auto a=read_pcm(argv[3]),b=read_pcm(argv[5]);
        auto text_a=read_transcript(argv[4]),text_b=read_transcript(argv[6]);
        ov_init_params init;
        ov_init_default_params(&init);
        init.model_path=argv[1];
        init.codec_path=argv[2];
        std::unique_ptr<ov_context,decltype(&ov_free)> context(ov_init(&init),ov_free);
        if(!context) throw std::runtime_error("ov_init failed");
        const auto identity=context.get();
        // Use the same prompt for A1/A2; B has separate audio, transcript, seed.
        const std::string probe_a="A short sentence checks persistent speech synthesis.";
        const std::string probe_b="The second reference tests state isolation.";
        const auto a1=synth(context.get(),a,text_a,probe_a,seed,speed);
        const auto b1=synth(context.get(),b,text_b,probe_b,(seed+1u)%2147483648ULL,speed);
        const auto a2=synth(context.get(),a,text_a,probe_a,seed,speed);
        if(identity!=context.get()) throw std::runtime_error("context identity changed");
        if(a1.canonical_pcm!=a2.canonical_pcm)
            throw std::runtime_error("A-B-A repeat differs at PCM16 level");
        if(a1.canonical_pcm==b1.canonical_pcm)
            throw std::runtime_error("distinct reference/seed produced identical PCM");
        context.reset(); // ov_free exactly once before successful exit
        std::cout<<"LITE_NATIVE_CONTEXT_REUSE_FEASIBILITY_PASS"
                 <<" init_count=1 synth_count=3 free_count=1"
                 <<" a_frames="<<a1.n_samples<<" b_frames="<<b1.n_samples
                 <<" a_repeat_equal=1 context_reused=1\n";
        return 0;
    }catch(const std::exception&e){
        // Do not echo paths, transcripts or private audio.
        std::cerr<<"LITE_NATIVE_CONTEXT_REUSE_FEASIBILITY_FAIL: "<<e.what()<<"\n";
        return 4;
    }
}
