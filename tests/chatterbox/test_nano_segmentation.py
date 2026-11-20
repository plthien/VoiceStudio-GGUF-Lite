"""Nano-only deterministic target-320/hard-600 segmentation branch coverage."""
import hashlib

import pytest

from lite.chatterbox_segmentation import (
    HARD_CHARS, TARGET_CHARS, POLICY_VERSION, SegmentationError, segment_text,
)


@pytest.mark.parametrize("length,expected", [
    (1, [1]), (520, [520]), (600, [600]), (601, [600, 1]),
    (1201, [600, 600, 1]), (4096, [600] * 6 + [496]),
])
def test_hard_boundaries_no_lost_chars(length, expected):
    text = "A" * length
    chunks = segment_text(text, 42)
    assert [len(chunk.text) for chunk in chunks] == expected
    assert "".join(chunk.text for chunk in chunks) == text
    assert all(len(chunk.text) <= HARD_CHARS for chunk in chunks)
    assert [chunk.sha256 for chunk in chunks] == [
        hashlib.sha256(chunk.text.encode()).hexdigest() for chunk in chunks]


def test_empty_whitespace_and_invalid_limit_rejected():
    for text in ("", "   \t"):
        with pytest.raises(SegmentationError):
            segment_text(text, 42)
    for args in ({"target": 0}, {"hard": 601}, {"target": 601}, {"target": 5, "hard": 4}):
        with pytest.raises(ValueError, match="INVALID_SEGMENT_LIMIT"):
            segment_text("Hello", 42, **args)


def test_sentence_priority_and_abbreviation_are_stable():
    text = "Dr. Smith paused. " + "界" * 530 + "。 Next sentence!"
    chunks = segment_text(text, 123)
    assert chunks[0].text == "Dr. Smith paused. "
    assert "".join(part.text for part in chunks) == text
    assert max(len(part.text) for part in chunks) <= 600
    assert [(s.text, s.sha256, s.seed) for s in chunks] == [
        (s.text, s.sha256, s.seed) for s in segment_text(text, 123)]
    assert all(s.seed is None for s in segment_text(text, None))
    assert [s.seed for s in chunks] != [s.seed for s in segment_text(text, 124)]
    assert POLICY_VERSION == "nano_sentence_v2"


def test_fallback_punctuation_and_whitespace_prefer_target():
    text = ("Longword, " * 65).rstrip()
    pieces = segment_text(text, 1)
    assert len(pieces) >= 2
    assert "".join(p.text for p in pieces) == text
    assert all(len(p.text) <= 600 for p in pieces)
    assert 280 <= len(pieces[0].text) <= 360


def test_supported_token_indivisible_on_hard_boundary():
    text = "A" * 598 + "[chuckle]" + " end."
    parts = segment_text(text, 1)
    assert "".join(p.text for p in parts) == text
    assert len(parts[0].text) == 598
    assert parts[1].text.startswith("[chuckle]")
    with pytest.raises(ValueError, match="UNSUPPORTED_PARALINGUISTIC_TAG"):
        segment_text("Words [laugh] are not allowed.", 1)
    assert segment_text("Look [CHUCKLE].", 1)


def test_custom_limits_and_punctuation_branch():
    assert [len(s.text) for s in segment_text("A" * 11, 1, target=5, hard=10)] == [10, 1]
    text = "abc, def, ghi, jkl"
    parts = segment_text(text, 1, target=7, hard=10)
    assert "".join(part.text for part in parts) == text
    assert all(len(part.text) <= 10 for part in parts)


def test_defensive_fail_closed_guards_and_empty_generator(monkeypatch):
    import lite.chatterbox_segmentation as mod
    with pytest.raises(SegmentationError):
        mod.segment_text("[chuckle]" + "Z" * 12, 1, target=3, hard=5)
    assert "".join(x.text for x in mod.segment_text("A " + "B" * 610, 1)) == "A " + "B" * 610
    assert "".join(x.text for x in mod.segment_text("A" * 400 + "[chuckle]" + "B" * 300, 1)) == "A" * 400 + "[chuckle]" + "B" * 300
    assert "".join(x.text for x in mod.segment_text("Hello. ", 1)) == "Hello. "
    monkeypatch.setattr(mod, "_bounded_sentences", lambda *args: iter(["Z" * 601]))
    with pytest.raises(SegmentationError):
        mod.segment_text("valid", 1)
    monkeypatch.setattr(mod, "_bounded_sentences", lambda *args: iter(["altered"]))
    with pytest.raises(RuntimeError, match="SEGMENTATION_INTEGRITY_FAILURE"):
        mod.segment_text("valid", 1)
    monkeypatch.setattr(mod, "_bounded_sentences", lambda *args: iter(()))
    with pytest.raises(RuntimeError, match="SEGMENTATION_INTEGRITY_FAILURE"):
        mod.segment_text("valid", 1)

def test_punctuation_without_whitespace_is_soft_boundary():
    text = "A" * 518 + "." + "B" * 100
    pieces = segment_text(text, 1)
    assert pieces[0].text.endswith(".")
    assert "".join(p.text for p in pieces) == text



def test_target_320_preserves_exact_477_char_section7_and_seed_lineage():
    # Regression from owner-observed Nano text omission on an earlier
    # 477-character single native inference call.
    text = (
        "\n\nFirst question: this man is talking to your partner. How jealous are you?\n\n"
        "Our big caveman takes one look at the skinny stranger.\n\n"
        "That's it. In his head, they're already sharing a cave. The stranger is raising his children. "
        "He's been demoted to the guy who delivers firewood.\n\n"
        "Nobody has even introduced themselves.\n\n"
        "Second question: this man wants to fight you. How threatened do you feel?\n\n"
        "Now the big guy rolls up his sleeves.\n\n"
        "Finally. A problem he knows how to solve.\n\n"
    )
    assert (TARGET_CHARS, HARD_CHARS, POLICY_VERSION) == (320, 600, "nano_sentence_v2")
    assert len(text) == 477
    chunks = segment_text(text, 817514548)
    assert [len(chunk.text) for chunk in chunks] == [320, 157]
    assert chunks[1].text.startswith("Second question:")
    assert "Finally. A problem he knows how to solve." in chunks[1].text
    assert "".join(chunk.text for chunk in chunks) == text
    assert all(len(chunk.text) <= HARD_CHARS for chunk in chunks)
    assert [chunk.sha256 for chunk in chunks] == [
        hashlib.sha256(chunk.text.encode("utf-8")).hexdigest() for chunk in chunks
    ]
    assert [(c.text, c.seed) for c in chunks] == [
        (c.text, c.seed) for c in segment_text(text, 817514548)
    ]
    assert [c.seed for c in chunks] != [
        c.seed for c in segment_text(text, 817514549)
    ]


def test_short_target_keeps_long_whole_sentence_up_to_hard_600():
    # Target is soft: never cut a sentence at 320 unless it exceeds hard 600.
    text = "A" * 450 + ". Next sentence."
    chunks = segment_text(text, 42)
    assert [len(chunk.text) for chunk in chunks] == [452, 14]
    assert "".join(chunk.text for chunk in chunks) == text
