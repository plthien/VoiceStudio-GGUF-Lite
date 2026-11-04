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
    assert result["maskgit_non_lm_overhead_us"]==result["maskgit_sampling_us"]
    assert result["maskgit_sampling_semantics"]=="derived_non_lm_overhead"

@pytest.mark.parametrize("steps",[32,24,16,12,8])
def test_effective_steps_allowed(steps):assert parse_performance(payload(steps))["maskgit_steps"]==steps

def test_invalid_or_future_payload_fails_closed():
    with pytest.raises(ValueError):parse_performance(payload(7))
    with pytest.raises(ValueError):parse_performance(b"bad")

from lite.performance_metrics import aggregate_performance


def test_multi_span_per_synth_totals_and_cumulative_cache_deltas():
    prior = {**parse_performance(payload()), "reference_cache_hits": 7,
             "reference_cache_misses": 2}
    spans = []
    for native_time, hits, misses, cache_hit in ((100, 8, 2, 1), (200, 8, 3, 0), (300, 9, 3, 1)):
        snap = {**prior, "native_total_us": native_time,
                "reference_cache_hits": hits, "reference_cache_misses": misses,
                "ref_cache_hit": cache_hit}
        spans.append(snap)
    result = aggregate_performance(spans, prior)
    assert result["span_count"] == 3
    assert result["native_total_us"] == 600
    assert result["model_load_us"] == 5000  # Retained, not 15000.
    assert result["reference_cache_hits"] == 2
    assert result["reference_cache_misses"] == 1
    assert result["ref_cache_hit_count"] == 2
    assert result["reference_codec_encode_calls"] == 3
    assert result["codec_decode_us"] == 90
    assert result["ref_codec_encode_us"] == 30


def test_multi_span_counter_reset_or_settings_change_fail_closed():
    first = parse_performance(payload())
    with pytest.raises(ValueError, match="regressed"):
        aggregate_performance([{**first, "reference_cache_hits": 6}], first)
    with pytest.raises(ValueError, match="settings"):
        aggregate_performance([first, {**first, "maskgit_steps": 8}])
    with pytest.raises(ValueError, match="empty"):
        aggregate_performance([])
