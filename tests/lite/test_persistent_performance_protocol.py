import struct,pytest
from lite.performance_metrics import MARKER,PAYLOAD,parse_performance

def payload(steps=32,fa=1):return MARKER+PAYLOAD.pack(1,PAYLOAD.size,0,5000,20,10,5,60,40,30,5,200,30,1,7,2,2400,9600,1,steps,fa,1)

def test_fixed_little_endian_versioned_perf_payload():
    result=parse_performance(payload())
    assert len(payload())==11+PAYLOAD.size and result["schema"]=="LitePerf/v1"
    assert result["ref_preprocess_us"]==20 and result["reference_cache_hits"]==7
    accounted=sum(result[name] for name in ("ref_preprocess_us","ref_codec_encode_us","prompt_and_duration_us",
        "maskgit_lm_forward_us","maskgit_sampling_us","codec_decode_us","postprocess_us","unaccounted_us"))
    assert accounted==result["native_total_us"]

@pytest.mark.parametrize("steps",[32,24,16,12,8])
def test_effective_steps_allowed(steps):assert parse_performance(payload(steps))["maskgit_steps"]==steps

def test_invalid_or_future_payload_fails_closed():
    with pytest.raises(ValueError):parse_performance(payload(7))
    with pytest.raises(ValueError):parse_performance(b"bad")
