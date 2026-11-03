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

# LitePerf/v1 mixes per-SYNTH durations with a retained load value and
# worker-cumulative reference-cache counters. Never sum all fields blindly.
PER_SYNTH_FIELDS = (
    "ref_preprocess_us", "ref_codec_encode_us", "prompt_and_duration_us",
    "maskgit_lm_forward_us", "maskgit_sampling_us", "codec_decode_us",
    "postprocess_us", "native_total_us", "unaccounted_us",
    "reference_codec_encode_calls", "encoded_ref_samples",
    "encoded_ref_bytes", "chunk_count",
)


def aggregate_performance(spans: list[dict], prior: dict | None = None) -> dict:
    """Aggregate a single multi-span request; `prior` is the last raw PERF frame.

    Cache hit/miss counters are cumulative within the current native context.
    A new resident context must pass prior=None, never a previous generation.
    """
    if not spans:
        raise ValueError("cannot aggregate an empty native performance request")
    for item in spans:
        if item.get("schema") != "LitePerf/v1":
            raise ValueError("native performance snapshot is unverified")
        if (item["maskgit_steps"] != spans[0]["maskgit_steps"] or
                item["flash_attention"] != spans[0]["flash_attention"]):
            raise ValueError("effective native settings changed across request spans")
    previous = prior or {"reference_cache_hits": 0, "reference_cache_misses": 0}
    for item in spans:
        if any(item[field] < previous[field] for field in
               ("reference_cache_hits", "reference_cache_misses")):
            raise ValueError("native cumulative cache counters regressed")
        previous = item
    first = spans[0]
    end = spans[-1]
    beginning = prior or {"reference_cache_hits": 0, "reference_cache_misses": 0}
    result = {"schema": "LitePerf/v1", "span_count": len(spans),
              "maskgit_steps": first["maskgit_steps"],
              "flash_attention": first["flash_attention"],
              "model_load_us": first["model_load_us"],
              "ref_cache_hit": int(any(item["ref_cache_hit"] for item in spans)),
              "ref_cache_hit_count": sum(item["ref_cache_hit"] for item in spans),
              "reference_cache_hits": end["reference_cache_hits"] - beginning["reference_cache_hits"],
              "reference_cache_misses": end["reference_cache_misses"] - beginning["reference_cache_misses"]}
    for field in PER_SYNTH_FIELDS:
        result[field] = sum(item[field] for item in spans)
    return result
