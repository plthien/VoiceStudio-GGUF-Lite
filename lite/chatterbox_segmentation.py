"""Deterministic provider-scoped sentence segmentation for Nano inference."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

TARGET_CHARS = 240
HARD_CHARS = 300
POLICY_VERSION = "nano_sentence_v1"
_BOUNDARY = re.compile(r"(?<=[.!?\u3002\uff01\uff1f])(?:[\"'\u2019\u201d\)\]]*)\s+")
_ABBREVIATION = re.compile(r"(?:\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|vs|etc)|(?:\b[A-Z]\.){1,4})\.$", re.I)
_TAG = re.compile(r"\[[^\]\r\n]{1,64}\]")
SUPPORTED_TAGS = frozenset({"[chuckle]"})


class SegmentationError(ValueError):
    code = "NANO_SEGMENT_SAFE_BOUNDARY_UNAVAILABLE"


@dataclass(frozen=True)
class Segment:
    text: str
    sha256: str
    seed: int | None


def _sentences(text: str) -> list[str]:
    if not text or not text.strip():
        raise SegmentationError()
    pieces, start = [], 0
    for match in _BOUNDARY.finditer(text):
        candidate = text[start:match.start()].rstrip()
        if _ABBREVIATION.search(candidate):
            continue
        pieces.append(text[start:match.end()])
        start = match.end()
    pieces.append(text[start:])
    return [piece for piece in pieces if piece]


def segment_text(text: str, seed: int | None, *, target: int = TARGET_CHARS,
                 hard: int = HARD_CHARS) -> tuple[Segment, ...]:
    if not (1 <= target <= hard <= HARD_CHARS):
        raise ValueError("INVALID_SEGMENT_LIMIT")
    if any(tag.group(0).lower() not in SUPPORTED_TAGS for tag in _TAG.finditer(text)):
        raise ValueError("UNSUPPORTED_PARALINGUISTIC_TAG")
    sentences = _sentences(text)
    if any(len(sentence) > hard for sentence in sentences):
        raise SegmentationError()
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if current and len(current) + len(sentence) > target:
            chunks.append(current)
            current = sentence
        else:
            current += sentence
        if len(current) > hard:
            raise SegmentationError()
    if current:
        chunks.append(current)
    result = []
    for index, chunk in enumerate(chunks):
        digest = hashlib.sha256(chunk.encode("utf-8")).hexdigest()
        derived = None if seed is None else int.from_bytes(hashlib.sha256(
            f"{POLICY_VERSION}\0{seed}\0{index}\0{digest}".encode()).digest()[:4], "big") & 0x7fffffff
        result.append(Segment(chunk, digest, derived))
    if "".join(segment.text for segment in result) != text:
        raise RuntimeError("SEGMENTATION_INTEGRITY_FAILURE")
    return tuple(result)
