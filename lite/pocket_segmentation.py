"""Lossless, deterministic source-span ledger for Pocket requests."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass

from .pause import COMPLETE, MARKER_START, parse_pause_script
from .pocket_registry import REGISTRY, MAX_INPUT_CHARS, MAX_CHUNK_CHARS, PocketError

POLICY = "pocket_multilingual_source_spans_v1"
WORD_POLICY = "pocket_unicode_orthographic_v1"
ABBREVIATIONS = {
    "es": frozenset(("sr.", "sra.", "dr.", "dra.", "ud.", "etc.")),
    "fr": frozenset(("m.", "mme.", "dr.", "etc.", "p.ex.")),
    "de": frozenset(("dr.", "prof.", "bzw.", "usw.", "z.b.")),
    "pt": frozenset(("sr.", "sra.", "dr.", "dra.", "etc.")),
    "it": frozenset(("sig.", "sig.ra.", "dott.", "prof.", "ecc.")),
    "nl": frozenset(("dhr.", "mevr.", "dr.", "prof.", "enz.")),
    "en": frozenset(("mr.", "mrs.", "ms.", "dr.", "prof.", "etc.", "e.g.", "i.e.")),
}
WORD = re.compile(r"[^\W_]+(?:[’'\-\u2010\u2011][^\W_]+)*", re.UNICODE)

@dataclass(frozen=True)
class Span:
    start: int
    end: int
    text: str
    kind: str
    pause_ms: int = 0

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8", "strict")).hexdigest()

def validate_text(text: str):
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_INPUT_CHARS:
        raise PocketError("INVALID_TEXT")
    try:
        text.encode("utf-8", "strict")
    except UnicodeError as exc:
        raise PocketError("INVALID_IPC_ENCODING") from exc

def _safe_boundary(text: str, pos: int) -> bool:
    if pos == len(text):
        return True
    # Never detach combining marks, variation selectors or joiner sequences.
    return (not unicodedata.category(text[pos]).startswith("M")
            and text[pos] != "\u200d" and text[pos - 1] != "\u200d")

def _sentence_boundary(prefix: str, language: str) -> bool:
    prefix = prefix.rstrip().rstrip("\"'”’»)]")
    if not prefix or prefix[-1] not in ".!?…":
        return False
    last = prefix.split()[-1].casefold()
    return last not in ABBREVIATIONS[language] and not re.fullmatch(r"(?:[^\W\d_]\.)+", last)

def _split(text: str, start: int, end: int, language: str) -> list[Span]:
    spans = []
    while start < end:
        stop = min(start + MAX_CHUNK_CHARS, end)
        if stop < end:
            # Boundaries immediately before/after whitespace retain every source character.
            candidates = [i for i in range(start + 1, stop + 1)
                          if (text[i-1].isspace() or text[i].isspace())
                          and _safe_boundary(text, i)]
            if not candidates:
                raise PocketError("TOKEN_TOO_LONG")
            sentences = [i for i in candidates if i >= start + MAX_CHUNK_CHARS // 2
                         and _sentence_boundary(text[start:i], language)]
            stop = max(sentences or candidates)
        part = text[start:stop]
        spans.append(Span(start, stop, part, "speech" if part.strip() else "space"))
        start = stop
    return spans

def build_job(text: str, language: str) -> tuple[Span, ...]:
    if language not in REGISTRY:
        raise PocketError("LANGUAGE_UNSUPPORTED")
    validate_text(text)
    # Existing parser is authority for the supported grammar, duration and count.
    parse_pause_script(text)
    for candidate in re.finditer(r"\[[ \t]*pause(?![A-Za-z0-9_])", text, re.I | re.ASCII):
        if COMPLETE.match(text, candidate.start()) is None:
            raise PocketError("INVALID_PAUSE_MARKER")
    spans = []
    start = 0
    for marker in MARKER_START.finditer(text):
        if marker.start() < start:
            continue
        complete = COMPLETE.match(text, marker.start())
        if complete is None:
            raise PocketError("INVALID_PAUSE_MARKER")
        spans.extend(_split(text, start, marker.start(), language))
        raw = complete.group()
        # Use the existing parser's exact integer frame policy.
        parsed = parse_pause_script("x" + raw)
        spans.append(Span(marker.start(), complete.end(), raw, "pause",
                          parsed.pause_frames // 24))
        start = complete.end()
    spans.extend(_split(text, start, len(text), language))
    if not any(s.kind == "speech" for s in spans):
        raise PocketError("INVALID_TEXT")
    return tuple(spans)

def word_count(spans: tuple[Span, ...]) -> int:
    # Join source spans, not chunks with inserted spaces; pause tags delimit words.
    text = "".join(s.text if s.kind != "pause" else " " for s in spans)
    # NFC is used only for word counting, never to mutate synthesis text.
    return len(WORD.findall(unicodedata.normalize("NFC", text)))
