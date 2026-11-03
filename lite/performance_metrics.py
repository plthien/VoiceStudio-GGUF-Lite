"""Parser for the optional fixed-size LitePerf/v1 private frame."""
import struct

MARKER=b"LitePerf/v1"
PAYLOAD=struct.Struct("<III16QIII")
FIELDS=("model_load_us","ref_preprocess_us","ref_codec_encode_us","prompt_and_duration_us",
        "maskgit_lm_forward_us","maskgit_sampling_us","codec_decode_us","postprocess_us",
        "native_total_us","unaccounted_us","reference_codec_encode_calls","reference_cache_hits",
        "reference_cache_misses","encoded_ref_samples","encoded_ref_bytes","chunk_count",
        "maskgit_steps","flash_attention","ref_cache_hit")

def parse_performance(payload:bytes)->dict:
    if len(payload)!=len(MARKER)+PAYLOAD.size or not payload.startswith(MARKER):
        raise ValueError("invalid LitePerf/v1 payload")
    version,size,flags,*values=PAYLOAD.unpack(payload[len(MARKER):])
    if version!=1 or size!=PAYLOAD.size or flags!=0:raise ValueError("unsupported LitePerf payload")
    result=dict(zip(FIELDS,values))
    if (result["maskgit_steps"] not in (32,24,16,12,8) or result["flash_attention"] not in (0,1)
            or result["ref_cache_hit"] not in (0,1)):
        raise ValueError("invalid LitePerf effective settings")
    stages=sum(result[name] for name in ("ref_preprocess_us","ref_codec_encode_us","prompt_and_duration_us",
               "maskgit_lm_forward_us","maskgit_sampling_us","codec_decode_us","postprocess_us","unaccounted_us"))
    if stages>result["native_total_us"]+1000:
        raise ValueError("LitePerf phase totals exceed native total")
    return {"schema":"LitePerf/v1",**result}
