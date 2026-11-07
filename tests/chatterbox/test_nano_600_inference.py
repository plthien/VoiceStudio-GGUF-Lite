"""Nano 520/600 inference, pause, API and incomplete native output smoke tests."""
import pytest

from lite.chatterbox_worker import checked_generated_audio
from lite.chatterbox_contract import decode_receipt
from .test_contracts import api, gen, profile


@pytest.mark.parametrize("reason", [
    "length", "max_length", "max_tokens", "max_new_tokens", "token_limit",
    "truncated", "out_of_tokens",
])
def test_runtime_explicit_token_exhaustion_is_rejected(reason):
    with pytest.raises(RuntimeError, match="NANO_GENERATION_TRUNCATED"):
        checked_generated_audio({"audio": object(), "finish_reason": reason})


@pytest.mark.parametrize("result", [
    {"audio": object(), "truncated": True},
    {"audio": object(), "is_truncated": True},
    {"audio": object(), "token_limit_reached": True},
])
def test_runtime_flagged_truncation_is_rejected(result):
    with pytest.raises(RuntimeError, match="NANO_GENERATION_TRUNCATED"):
        checked_generated_audio(result)


@pytest.mark.parametrize("result", [
    {"audio": object(), "failed": True},
    {"audio": object(), "success": False},
    {"audio": object(), "error": "FAILED"},
    {"audio": object(), "finish_reason": "unexpected"},
    {"audio": None, "finish_reason": "eos"},
    (None, {"stop_reason": "eos"}),
])
def test_runtime_failed_or_unknown_status_is_rejected(result):
    with pytest.raises(RuntimeError, match="NANO_GENERATION_FAILED"):
        checked_generated_audio(result)


def test_plain_tensor_and_explicit_completion_pass_through():
    wav = object()
    assert checked_generated_audio(wav) is wav
    assert checked_generated_audio({"audio": wav, "finish_reason": "eos"}) is wav
    assert checked_generated_audio((wav, {"stop_reason": "completed"})) is wav


@pytest.mark.parametrize("length,expected", [(520, [520]), (600, [600]), (601, [600, 1])])
def test_real_service_calls_worker_with_native_boundaries(api, length, expected):
    client, service, _ = api
    row = profile(client)
    original_rpc = service._rpc
    observed = []

    async def capture(msg, deadline):
        observed.append(msg["text"])
        return await original_rpc(msg, deadline)

    service._rpc = capture
    text = "A" * length
    response = gen(client, row["id"], text)
    assert response.status_code == 200, response.text
    assert [len(part) for part in observed] == expected
    assert "".join(observed) == text
    assert response.headers["X-TTS-Native-Chunks"] == str(len(expected))
    receipt = decode_receipt(response.headers["X-TTS-Execution-Receipt"],
                             proof=response.headers["X-TTS-Execution-Proof"], wav=response.content)
    assert receipt["native_chunk_count"] == len(expected)
    assert receipt["segmentation_policy"] == "nano_sentence_v2"


def test_pause_script_keeps_exact_order_and_frames_through_multiple_chunks(api):
    client, service, _ = api
    row = profile(client)
    original_rpc = service._rpc
    observed = []

    async def capture(msg, deadline):
        observed.append(msg["text"])
        return await original_rpc(msg, deadline)

    service._rpc = capture
    first, second, last = "A" * 601, "B" * 601, "Last."
    text = first + " [pause 800ms] " + second + " [pause 1.5s] " + last
    response = gen(client, row["id"], text)
    assert response.status_code == 200, response.text
    assert "".join(observed) == first + second + last
    assert [len(part) for part in observed] == [600, 1, 600, 1, len(last)]
    assert response.headers["X-Lite-Speech-Segments"] == "3"
    assert response.headers["X-TTS-Native-Chunks"] == "5"
    receipt = decode_receipt(response.headers["X-TTS-Execution-Receipt"],
                             proof=response.headers["X-TTS-Execution-Proof"], wav=response.content)
    assert receipt["pause_count"] == 2
    assert receipt["authored_pause_frames"] == 2300 * 24


def test_invalid_api_request_length_does_not_expand_with_native_chunk_limit(api):
    client, service, _ = api
    row = profile(client)
    assert gen(client, row["id"], "A" * 4097).status_code == 422
    assert service.syntheses == 0


@pytest.mark.parametrize("flag,code", [
    ({"generation_complete": False}, "NANO_GENERATION_TRUNCATED"),
    ({"truncated": True}, "NANO_GENERATION_TRUNCATED"),
    ({"failed": True}, "NANO_GENERATION_FAILED"),
])
def test_service_fails_closed_without_publishing_truncated_audio(api, flag, code):
    client, service, _ = api
    row = profile(client)
    original_rpc = service._rpc

    async def incomplete(msg, deadline):
        answer = await original_rpc(msg, deadline)
        return {**answer, **flag}

    service._rpc = incomplete
    response = gen(client, row["id"], "Short sentence.")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == code
    assert service.state == "ERROR"
