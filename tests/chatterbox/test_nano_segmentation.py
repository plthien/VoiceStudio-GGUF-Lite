import pytest

from lite.chatterbox_segmentation import segment_text, SegmentationError, HARD_CHARS


def test_300_character_sentence_is_one_native_segment():
    text = "A" * (HARD_CHARS - 1) + "."
    parts = segment_text(text, 42)
    assert len(parts) == 1 and parts[0].text == text


def test_301_character_sentence_without_boundary_is_rejected():
    with pytest.raises(SegmentationError, match=""):
        segment_text("A" * 300 + ".", 42)


def test_sentence_order_unicode_abbreviation_and_seed_are_stable():
    text = "Dr. Smith paused. " + "界" * 210 + "。 Next sentence!"
    first = segment_text(text, 123)
    second = segment_text(text, 123)
    assert "".join(item.text for item in first) == text
    assert [(x.sha256, x.seed) for x in first] == [(x.sha256, x.seed) for x in second]
    assert all(len(item.text) <= 300 for item in first)


def test_only_verified_nano_paralinguistic_token_is_accepted():
    assert segment_text("That is funny [chuckle].", 1)
    with pytest.raises(ValueError, match="UNSUPPORTED_PARALINGUISTIC_TAG"):
        segment_text("That is funny [laugh].", 1)
