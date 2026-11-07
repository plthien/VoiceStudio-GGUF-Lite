"""Deterministic provider-scoped sentence segmentation for Nano inference."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

TARGET_CHARS = 520
HARD_CHARS = 600
POLICY_VERSION = "nano_sentence_v2"
_BOUNDARY = re.compile(r"(?<=[.!?\u3002\uff01\uff1f])(?:[\"'\u2019\u201d\)\]]*)\s+")
_ABBREVIATION = re.compile(r"(?:\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|vs|etc)|(?:\b[A-Z]\.){1,4})\.$", re.I)
_FALLBACK = re.compile(r"[,;:\u2014\u2013]\s*|\s+")
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


def _safe_cut(text: str, target: int, hard: int) -> int:
    """Find a non-destructive soft boundary; cut hard only for unbroken text."""
    candidates = [m.end() for m in _FALLBACK.finditer(text[:hard]) if m.end() > 0]
    viable = [pos for pos in candidates if pos >= max(1, target // 2)]
    cut = min(viable, key=lambda pos: (abs(pos - target), -pos)) if viable else (
        candidates[-1] if candidates else hard)
    # A supported paralinguistic token is indivisible even across a hard cut.
    for token in _TAG.finditer(text):
        if token.start() < cut < token.end():
            cut = token.start() if token.start() > 0 else token.end()
            break
    if not 0 < cut <= hard:
        raise SegmentationError()
    return cut


def _bounded_sentences(text: str, target: int, hard: int):
    for sentence in _sentences(text):
        while len(sentence) > hard:
            cut = _safe_cut(sentence, target, hard)
            yield sentence[:cut]
            sentence = sentence[cut:]
        yield sentence


def segment_text(text: str, seed: int | None, *, target: int = TARGET_CHARS,
                 hard: int = HARD_CHARS) -> tuple[Segment, ...]:
    if not (1 <= target <= hard <= HARD_CHARS):
        raise ValueError("INVALID_SEGMENT_LIMIT")
    if any(tag.group(0).lower() not in SUPPORTED_TAGS for tag in _TAG.finditer(text)):
        raise ValueError("UNSUPPORTED_PARALINGUISTIC_TAG")
    chunks: list[str] = []
    current = ""
    for sentence in _bounded_sentences(text, target, hard):
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
