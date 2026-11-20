"""Request-level Vietnamese segmentation; VieNeu retains native 140-char splitting."""
from __future__ import annotations
from dataclasses import dataclass
from .pause import PauseScript, SpeechPart, PausePart, parse_pause_script

POLICY = "vi_request_sentence_700_sdk140_v1"
MAX_INPUT_CHARS = 24000
MAX_PART_CHARS = 700

def split_speech(text: str, limit: int = MAX_PART_CHARS) -> list[str]:
    if not text or len(text) > MAX_INPUT_CHARS:
        raise ValueError("INVALID_TEXT")
    chunks: list[str] = []
    remaining = text.strip()
    if not remaining:
        raise ValueError("INVALID_TEXT")
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        candidates = [remaining.rfind(c, limit // 2, limit) for c in (". ", "? ", "! ", "; ", "\n", " ")]
        end = max(candidates) + 1
        if end <= 0:
            end = limit
        part, remaining = remaining[:end].strip(), remaining[end:].lstrip()
        if part:
            chunks.append(part)
    return chunks

@dataclass(frozen=True)
class ParsedJob:
    script: PauseScript
    spoken: tuple[str, ...]

def build_job(text: str) -> ParsedJob:
    if len(text) > MAX_INPUT_CHARS:
        raise ValueError("INVALID_TEXT")
    parsed = parse_pause_script(text)
    if parsed is None:
        parts = (SpeechPart(text.strip()),)
        parsed = PauseScript(parts, 0, 0, 1)
    expanded = []
    words = []
    for p in parsed.parts:
        if isinstance(p, PausePart):
            expanded.append(p)
        else:
            for chunk in split_speech(p.text):
                expanded.append(SpeechPart(chunk))
                words.append(chunk)
    script = PauseScript(tuple(expanded), parsed.pause_count, parsed.pause_frames,
                         len(words))
    if not words:
        raise ValueError("INVALID_TEXT")
    return ParsedJob(script, tuple(words))
