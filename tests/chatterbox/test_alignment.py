"""Wav2Vec2 forced-alignment contract tests (mock CI: no Torch, no weights).

The pure text/token mapping is tested directly. The HTTP surface is
tested with a stub aligner seeded into app state, following the same
ElevenLabs response shape the real engine produces.
"""
import io
import wave

import pytest
from fastapi.testclient import TestClient

from lite.alignment_engine import (AlignmentError, TokenSpan,
                                   characters_from_spans, plan_characters,
                                   words_from_characters)
from lite.chatterbox_app import create_app
from lite.chatterbox_config import ChatterboxSettings
from .test_contracts import FakeTurbo


def _spans(tokens, step=0.1):
    spans, separators = [], {}
    for i, token in enumerate(tokens):
        span = TokenSpan(token=token, start=i * step, end=(i + 1) * step,
                         score=-0.25)
        spans.append(span)
        if token == "|":
            separators[i] = span
    return spans, separators


def _characters(text, duration=10.0):
    tokens, plans = plan_characters(text)
    spans, separators = _spans(tokens)
    return characters_from_spans(text, tokens, plans, spans, separators,
                                 duration)


def test_plan_tokens_normalizes_case_and_whitespace():
    tokens, plans = plan_characters("Hello  world")
    assert tokens == list("HELLO|WORLD")
    assert len(plans) == len("Hello  world")
    kinds = [plan.kind for plan in plans]
    assert kinds.count("token") == 10
    assert kinds.count("separator") == 2  # both spaces share one "|" span


def test_characters_reconstruct_exact_text_with_punctuation():
    text = "Hello, world! It's 2026."
    characters = _characters(text)
    assert "".join(c["text"] for c in characters) == text
    starts = [c["start"] for c in characters]
    ends = [c["end"] for c in characters]
    assert starts == sorted(starts) and ends == sorted(ends)
    assert all(s <= e for s, e in zip(starts, ends))
    assert characters[-1]["end"] <= 10.0


def test_characters_leading_and_trailing_fill():
    characters = _characters("...Hello...", duration=5.0)
    assert characters[0]["start"] == 0.0
    assert "".join(c["text"] for c in characters) == "...Hello..."


def test_words_grouping_and_losses():
    text = "Hello world"
    characters = _characters(text)
    losses = [0.5 if plan.kind == "token" else None
              for plan in plan_characters(text)[1]]
    words = words_from_characters(text, characters, losses, 0.5)
    assert [w["text"] for w in words] == ["Hello", "world"]
    assert words[0]["start"] == characters[0]["start"]
    assert words[1]["end"] == characters[-1]["end"]
    assert all(w["type"] == "word" for w in words)


def test_plan_rejects_text_without_tokens():
    tokens, plans = plan_characters("123 ...")
    assert not any(plan.kind == "token" for plan in plans)
    assert tokens == []


class StubAligner:
    loaded = True
    device = "cpu"

    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def align(self, wav_bytes, text):
        self.calls.append((wav_bytes, text))
        if self.error is not None:
            raise self.error
        characters = [{"text": ch, "start": i * 0.05, "end": (i + 1) * 0.05}
                      for i, ch in enumerate(text)]
        return {"characters": characters,
                "words": [{"text": text, "start": 0.0,
                           "end": len(text) * 0.05, "type": "word",
                           "loss": 0.25}],
                "loss": 0.25}


@pytest.fixture
def api(tmp_path):
    settings = ChatterboxSettings(root=tmp_path, timeout=0.7)
    application = create_app(settings=settings, service=FakeTurbo(settings))
    with TestClient(application) as client:
        yield client, application


def _wav_bytes(seconds=0.5, rate=24000):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(seconds * rate))
    return buffer.getvalue()


def test_capability_reports_engine_honestly(api):
    client, _ = api
    body = client.get("/lite/alignment-capability").json()
    assert body["contract_version"] == "wav2vec2_forced_alignment_v1"
    assert body["provider_route"] == "POST /v1/forced-alignment"
    assert body["response_shape"] == "elevenlabs_forced_alignment_compatible"
    assert body["model_loaded"] is False  # capability never loads the model


def test_forced_alignment_route_roundtrip(api):
    client, application = api
    stub = StubAligner()
    application.state.alignment_aligner = stub
    response = client.post(
        "/v1/forced-alignment",
        files={"file": ("section.wav", _wav_bytes(), "audio/wav")},
        data={"text": "Hello world."})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert "".join(c["text"] for c in payload["characters"]) == "Hello world."
    assert payload["loss"] == 0.25
    assert stub.calls and stub.calls[0][1] == "Hello world."


def test_forced_alignment_route_rejects_blank_text(api):
    client, application = api
    application.state.alignment_aligner = StubAligner()
    response = client.post(
        "/v1/forced-alignment",
        files={"file": ("section.wav", _wav_bytes(), "audio/wav")},
        data={"text": "   "})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ALIGNMENT_TEXT_INVALID"


def test_forced_alignment_route_maps_engine_errors(api):
    client, application = api
    application.state.alignment_aligner = StubAligner(
        error=AlignmentError("ALIGNMENT_ENGINE_UNAVAILABLE"))
    response = client.post(
        "/v1/forced-alignment",
        files={"file": ("section.wav", _wav_bytes(), "audio/wav")},
        data={"text": "Hello."})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ALIGNMENT_ENGINE_UNAVAILABLE"
