"""Pure bounded inline-pause parser. No generic SSML / emotion tag rewriting."""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

MAX_MARKERS = 16
MAX_TOTAL_MS = 60_000
MARKER_START = re.compile(r"\[[ \t]*pause(?=[ \t\]\r\n]|$)", re.I | re.ASCII)
COMPLETE = re.compile(r"\[[ \t]*pause(?:[ \t]+([0-9]+(?:\.[0-9]+)?)[ \t]*(ms|s))?[ \t]*\]", re.I | re.ASCII)

class PauseError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

@dataclass(frozen=True)
class SpeechPart:
    text: str

@dataclass(frozen=True)
class PausePart:
    milliseconds: int

@dataclass(frozen=True)
class PauseScript:
    parts: tuple[SpeechPart | PausePart, ...]
    pause_count: int
    pause_frames: int
    speech_segments: int

def parse_pause_script(text: str) -> PauseScript | None:
    """None means *unchanged* native fast path; no other bracket tag is rewritten.

    For tagged inputs only, whitespace at each speech span's leading/trailing
    boundary is stripped; internal whitespace and all punctuation are preserved.
    An unclosed/malformed [pause...] is a typed error, never sent as narration.
    """
    if not MARKER_START.search(text):
        return None
    result: list[SpeechPart | PausePart] = []
    end = 0
    count = 0
    total_ms = 0
    for marker in MARKER_START.finditer(text):
        if marker.start() < end:
            continue
        spoken = text[end:marker.start()].strip()
        if spoken:
            result.append(SpeechPart(spoken))
        complete = COMPLETE.match(text, marker.start())
        if not complete:
            raise PauseError("INVALID_PAUSE_MARKER")
        value, unit = complete.group(1), complete.group(2)
        if value is None:
            ms = 350
        else:
            # Avoid unbounded integer/Decimal work on a hostile, 4096-char value:
            # values exceeding 10 seconds can be rejected lexically first.
            if len(value) > (6 if unit.lower()=="ms" else 8):
                raise PauseError("INVALID_PAUSE_MARKER")
            # The grammar forbids exponents/NaN and over-precise decimals.
            if unit.lower() == "ms":
                if "." in value:
                    raise PauseError("INVALID_PAUSE_MARKER")
                ms = int(value)
            else:
                if "." in value and len(value.split(".", 1)[1]) > 3:
                    raise PauseError("INVALID_PAUSE_MARKER")
                try:
                    msec = Decimal(value) * Decimal(1000)
                except InvalidOperation as exc:
                    raise PauseError("INVALID_PAUSE_MARKER") from exc
                if msec != msec.to_integral_value():
                    raise PauseError("INVALID_PAUSE_MARKER")
                ms = int(msec)
            if not 1 <= ms <= 10_000:
                raise PauseError("INVALID_PAUSE_MARKER")
        count += 1
        total_ms += ms
        if count > MAX_MARKERS or total_ms > MAX_TOTAL_MS:
            raise PauseError("PAUSE_LIMIT_EXCEEDED")
        result.append(PausePart(ms))
        end = complete.end()
    tail = text[end:].strip()
    if tail:
        result.append(SpeechPart(tail))
    speeches = sum(isinstance(x, SpeechPart) for x in result)
    if not speeches:
        raise PauseError("INVALID_REQUEST")
    return PauseScript(tuple(result), count, total_ms * 24, speeches)

def speech_text(script: PauseScript | None, raw: str) -> str:
    return raw if script is None else " ".join(x.text for x in script.parts if isinstance(x, SpeechPart))
