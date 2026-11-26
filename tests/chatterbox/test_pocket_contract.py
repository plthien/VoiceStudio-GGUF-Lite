"""Host-only tests; do not claim model, installer or Windows acceptance."""
import math
import shutil
import struct

import pytest
from pydantic import ValidationError

from lite.pocket_registry import REGISTRY, PocketError, resolve_model, discovery_entry
from lite.pocket_segmentation import build_job, word_count
from lite.pocket_contract import PocketSpeech
from lite.pocket_audio import Assembly, apply_speed, encode_pcm, inspect_wav

EXPECTED = {
    "es": ("spanish_24l", 24, "¿Qué pasó, Sr. Núñez? ¡Sí!"),
    "fr": ("french_24l", 24, "M. Noël aime l’été et le cœur."),
    "de": ("german_24l", 24, "Dr. Müller fährt über die Straße."),
    "pt": ("portuguese_24l", 24, "Sr. João vê a ação."),
    "it": ("italian_24l", 24, "Il dott. Rossi è già qui."),
    "nl": ("dutch_24l", 24, "Dhr. De Vries houdt van geëmailleerd aardewerk."),
    "en": ("english_2026-10", 6, "Dr. Smith said: don’t stop."),
}

def test_exact_registry():
    assert list(REGISTRY) == list(EXPECTED)
    assert discovery_entry()["languages"] == list(EXPECTED)
    for code, (variant, layers, _) in EXPECTED.items():
        spec = resolve_model(code, variant)
        assert spec.layers == layers
        assert spec.sample_rate == 24000
        assert spec.precision_policy == ("fp32",)
        assert len(spec.config_git_blob) == 40
        assert len(spec.model_revision) == 40

@pytest.mark.parametrize("code", EXPECTED)
def test_lossless_and_contract(code):
    variant, _, sentence = EXPECTED[code]
    source = "  " + (sentence + " \n") * 30 + "[pause 800ms]" + sentence + " [pause 1.5s] "
    req = PocketSpeech(language=code, model_variant=variant, voice="fixture", input=source)
    assert req.input == source
    spans = build_job(source, code)
    assert "".join(s.text for s in spans) == source
    assert all(source[s.start:s.end] == s.text for s in spans)
    assert all(a.end == b.start for a, b in zip(spans, spans[1:]))
    assert all(len(s.text) <= 320 for s in spans if s.kind == "speech")
    assert sum(s.pause_ms for s in spans) == 2300
    assert word_count(spans) > 30

@pytest.mark.parametrize("patch", [
    {"language": "vi"}, {"model_variant": "french_24l"},
    {"model_variant": "spanish"}, {"speed": True}, {"speed": "1"},
    {"speed": float("nan")}, {"speed": 1.26}, {"extra": 1},
    {"input": "\ud800"}, {"input": " "}, {"response_format": "mp3"},
])
def test_strict_contract(patch):
    data = {"language": "es", "model_variant": "spanish_24l",
            "voice": "fixture", "input": "Hola."}
    with pytest.raises(ValidationError):
        PocketSpeech.model_validate({**data, **patch})

@pytest.mark.parametrize("field", ["language", "model_variant", "voice", "input"])
def test_required(field):
    data = {"language": "es", "model_variant": "spanish_24l",
            "voice": "fixture", "input": "Hola."}
    del data[field]
    with pytest.raises(ValidationError):
        PocketSpeech.model_validate(data)

@pytest.mark.parametrize("code", EXPECTED)
def test_reject_overlong_token(code):
    with pytest.raises(PocketError, match="TOKEN_TOO_LONG"):
        build_job("a" * 321, code)

@pytest.mark.parametrize("text", ["Hola [pause=3s]", "Hola [pause 12s]", "Hola [pause 1.2ms]", "[pause]"])
def test_invalid_pauses(text):
    with pytest.raises(ValueError):
        build_job(text, "es")

def test_count_conventions():
    spans = build_job("  l’été co-op Straße Überlebensstrategie e\u0301té 2026 [pause] ", "fr")
    assert word_count(spans) == 6

def fixture_wav(seconds=1):
    pcm = b"".join(struct.pack("<h", int(8000 * math.sin(i * 2 * math.pi * 440 / 24000)))
                   for i in range(seconds * 24000))
    return encode_pcm(pcm)

def test_exact_pause_assembly():
    wav = fixture_wav()
    _, pcm = inspect_wav(wav)
    out = Assembly()
    out.speech(wav)
    out.pause(350)
    out.pause(800)
    out.pause(1500)
    out.speech(wav)
    data, info = out.finish()
    assert info.frames == 24000 * 2 + 2650 * 24
    assert inspect_wav(data)[1] == pcm + b"\0" * (2650 * 48) + pcm
    assert apply_speed(wav, 1.0) == wav

def test_pcm_rejection():
    for bad in (b"", fixture_wav() + b"x", encode_pcm(b"\0" * 20),
                encode_pcm(b"\xff\x7f" * 20)):
        with pytest.raises(PocketError):
            inspect_wav(bad)
    with pytest.raises(PocketError):
        apply_speed(fixture_wav(), True)

@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg unavailable")
def test_real_ffmpeg_speed():
    wav = fixture_wav(3)
    fast = inspect_wav(apply_speed(wav, 1.25, shutil.which("ffmpeg")))[0]
    slow = inspect_wav(apply_speed(wav, 0.8, shutil.which("ffmpeg")))[0]
    assert 2.2 < fast.seconds < 2.6
    assert 3.5 < slow.seconds < 4.0
