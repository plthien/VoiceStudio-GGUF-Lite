"""Wav2Vec2 forced alignment engine (ElevenLabs-compatible output shape).

Aligns a KNOWN transcript to WAV audio with torchaudio's CTC forced
alignment over the WAV2VEC2_ASR_BASE_960H bundle, then maps token spans
back onto every character of the original transcript so the concatenated
character texts reconstruct the input text exactly (the contract the
YouTube AI Studio voice pipeline validates).

Design rules, following this repo's conventions:
- No torch/torchaudio import at module level (the layout check forbids it
  and mock CI never installs Torch). Heavy imports happen lazily inside
  the aligner, on first use.
- The text/token mapping half of the engine is pure Python so it is fully
  testable without Torch.
- English model: the bundle vocabulary is A-Z plus apostrophe. Digits and
  punctuation carry no acoustic token; their timings are interpolated
  between neighbouring aligned characters (documented limitation).
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass

ENGINE_NAME = "wav2vec2"
MODEL_BUNDLE = "WAV2VEC2_ASR_BASE_960H"
MODEL_SAMPLE_RATE = 16000
CONTRACT_VERSION = "wav2vec2_forced_alignment_v1"
DEVICE_ENV = "VOICESTUDIO_ALIGNMENT_DEVICE"

# Characters the 960H dictionary can align acoustically: letters and the
# apostrophe. Everything else is interpolated.
_TOKEN_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ'")


class AlignmentError(Exception):
    """Alignment failure carrying a stable machine-readable code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class TokenSpan:
    """One aligned model token, in seconds, with its mean log-prob score."""

    token: str
    start: float
    end: float
    score: float


@dataclass
class _CharPlan:
    """How one original character obtains its timing."""

    kind: str  # "token" | "separator" | "fill"
    token_index: int = -1  # index into token spans for kind == "token"
    separator_index: int = -1  # index into separator spans for "separator"


def plan_characters(text: str) -> tuple[list[str], list[_CharPlan]]:
    """Split *text* into the model token stream plus a per-character plan.

    Returns (tokens, plans): tokens is the normalized stream fed to forced
    alignment (uppercase letters/apostrophes, with "|" for a whitespace
    run); plans has exactly one entry per character of *text*.
    """
    tokens: list[str] = []
    plans: list[_CharPlan] = []
    pending_separator: int = -1  # token index of an unconsumed "|"
    for ch in text:
        upper = ch.upper()
        if len(upper) == 1 and upper in _TOKEN_CHARS:
            plans.append(_CharPlan("token", token_index=len(tokens)))
            tokens.append(upper)
            pending_separator = -1
        elif ch.isspace():
            if tokens and pending_separator < 0:
                pending_separator = len(tokens)
                tokens.append("|")
                plans.append(_CharPlan("separator", separator_index=pending_separator))
            else:
                # Extra whitespace in a run (or leading whitespace) shares
                # the run's separator span; leading whitespace has none and
                # is interpolated from the first token instead.
                plans.append(_CharPlan(
                    "separator" if pending_separator >= 0 else "fill",
                    separator_index=pending_separator))
        else:
            plans.append(_CharPlan("fill"))
    return tokens, plans


def characters_from_spans(
    text: str,
    tokens: list[str],
    plans: list[_CharPlan],
    token_spans: list[TokenSpan],
    separator_spans: dict[int, TokenSpan],
    duration_seconds: float,
) -> list[dict]:
    """Build the ElevenLabs-shaped character list for *text*.

    token_spans[i] is the aligned span of tokens[i] when it is a letter
    token; separator_spans maps a "|" token index to its span. Fill
    characters (punctuation, digits, symbols) are interpolated linearly
    between the surrounding anchored characters.
    """
    if len(token_spans) != len(tokens):
        raise AlignmentError("ALIGNMENT_FAILED")
    anchored: list[tuple[float, float] | None] = []
    for plan in plans:
        if plan.kind == "token":
            span = token_spans[plan.token_index]
            anchored.append((span.start, span.end))
        elif plan.kind == "separator" and plan.separator_index in separator_spans:
            span = separator_spans[plan.separator_index]
            anchored.append((span.start, span.end))
        else:
            anchored.append(None)
    # Interpolate runs of unanchored characters between their neighbours.
    resolved: list[tuple[float, float]] = []
    index = 0
    total = len(anchored)
    previous_end = 0.0
    while index < total:
        if anchored[index] is not None:
            start, end = anchored[index]  # type: ignore[misc]
            resolved.append((start, end))
            previous_end = end
            index += 1
            continue
        run_end = index
        while run_end < total and anchored[run_end] is None:
            run_end += 1
        next_start = anchored[run_end][0] if run_end < total else duration_seconds
        span_start, span_end = previous_end, max(next_start, previous_end)
        count = run_end - index
        for offset in range(count):
            start = span_start + (span_end - span_start) * offset / count
            end = span_start + (span_end - span_start) * (offset + 1) / count
            resolved.append((start, end))
        index = run_end
    # Enforce the consumer's invariants: finite, monotonic, in range.
    characters: list[dict] = []
    cursor_start = 0.0
    cursor_end = 0.0
    for ch, (start, end) in zip(text, resolved):
        start = min(max(start, cursor_start, 0.0), duration_seconds)
        end = min(max(end, start, cursor_end if end < cursor_end else end), duration_seconds)
        if end < start:
            end = start
        characters.append({"text": ch, "start": round(start, 6), "end": round(end, 6)})
        cursor_start, cursor_end = start, end
    return characters


def words_from_characters(text: str, characters: list[dict],
                          char_losses: list[float | None],
                          default_loss: float) -> list[dict]:
    """Group character timings into ElevenLabs-shaped word entries."""
    words: list[dict] = []
    cursor = 0
    length = len(text)
    while cursor < length:
        if text[cursor].isspace():
            cursor += 1
            continue
        start_cursor = cursor
        while cursor < length and not text[cursor].isspace():
            cursor += 1
        chunk = characters[start_cursor:cursor]
        losses = [char_losses[i] for i in range(start_cursor, cursor)
                  if char_losses[i] is not None]
        words.append({
            "text": text[start_cursor:cursor],
            "start": chunk[0]["start"],
            "end": chunk[-1]["end"],
            "type": "word",
            "loss": round(sum(losses) / len(losses), 6) if losses else default_loss,
        })
    return words


def engine_available() -> bool:
    """True when torchaudio is importable, without importing Torch here."""
    import importlib.util
    return importlib.util.find_spec("torchaudio") is not None


def requested_device() -> str:
    value = os.environ.get(DEVICE_ENV, "cpu").strip().lower()
    return value if value in ("cpu", "cuda") else "cpu"


class Wav2Vec2Aligner:
    """Lazily loaded, lock-serialized Wav2Vec2 forced aligner."""

    def __init__(self, device: str | None = None):
        self._device_request = device or requested_device()
        self._device = "cpu"
        self._model = None
        self._labels: tuple[str, ...] = ()
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def device(self) -> str:
        return self._device

    def _load(self):
        if self._model is not None:
            return
        try:
            import torch
            import torchaudio
        except ImportError as exc:
            raise AlignmentError("ALIGNMENT_ENGINE_UNAVAILABLE") from exc
        bundle = torchaudio.pipelines.WAV2VEC2_ASR_BASE_960H
        device = self._device_request
        if device == "cuda" and not torch.cuda.is_available():
            device = "cpu"
        model = bundle.get_model().to(device)
        model.eval()
        self._labels = tuple(bundle.get_labels())
        self._device = device
        self._model = model

    def align(self, wav_bytes: bytes, text: str) -> dict:
        """Align *text* to WAV *wav_bytes*; return the response payload."""
        if not text or not text.strip():
            raise AlignmentError("ALIGNMENT_TEXT_INVALID")
        tokens, plans = plan_characters(text)
        if not any(plan.kind == "token" for plan in plans):
            raise AlignmentError("ALIGNMENT_TEXT_INVALID")
        with self._lock:
            self._load()
            token_spans, separator_spans, duration, mean_loss, char_losses = (
                self._align_locked(wav_bytes, text, tokens, plans))
        characters = characters_from_spans(
            text, tokens, plans, token_spans, separator_spans, duration)
        words = words_from_characters(text, characters, char_losses, mean_loss)
        return {"characters": characters, "words": words,
                "loss": round(mean_loss, 6)}

    def _align_locked(self, wav_bytes: bytes, text: str, tokens: list[str],
                      plans: list[_CharPlan]):
        import torch
        import torchaudio

        waveform, sample_rate, duration = _decode_wav(wav_bytes)
        if sample_rate != MODEL_SAMPLE_RATE:
            waveform = torchaudio.transforms.Resample(
                sample_rate, MODEL_SAMPLE_RATE)(waveform)
        waveform = waveform.to(self._device)
        with torch.inference_mode():
            emissions, _ = self._model(waveform.unsqueeze(0))
            log_probs = torch.log_softmax(emissions[0], dim=-1)
        dictionary = {label: idx for idx, label in enumerate(self._labels)}
        try:
            target_ids = [dictionary[token] for token in tokens]
        except KeyError as exc:
            raise AlignmentError("ALIGNMENT_TEXT_INVALID") from exc
        targets = torch.tensor([target_ids], dtype=torch.int32,
                               device=log_probs.device)
        try:
            aligned, scores = torchaudio.functional.forced_align(
                log_probs.unsqueeze(0), targets,
                input_lengths=torch.tensor([log_probs.shape[0]]),
                target_lengths=torch.tensor([len(target_ids)]))
        except RuntimeError as exc:
            raise AlignmentError("ALIGNMENT_FAILED") from exc
        aligned = aligned[0].tolist()
        scores = scores[0].tolist()
        frames = len(aligned)
        if frames == 0:
            raise AlignmentError("ALIGNMENT_FAILED")
        ratio = duration / frames
        token_spans, separator_spans, char_losses = _merge_frames(
            tokens, aligned, scores, ratio, plans, text)
        losses = [span.score for span in token_spans if span.token != "|"]
        mean_loss = -sum(losses) / len(losses) if losses else 0.0
        return token_spans, separator_spans, duration, mean_loss, char_losses


def _merge_frames(tokens, aligned, scores, ratio, plans, text):
    """Merge per-frame forced-align output into per-token spans.

    Returns (token_spans, separator_spans, char_losses): token_spans is
    parallel to *tokens* (letters and "|" alike); separator_spans maps a
    "|" token index to its span; char_losses parallels *text* with the
    per-character loss for acoustically aligned characters, else None.
    """
    spans: list[TokenSpan | None] = [None] * len(tokens)
    cursor = 0  # index into tokens as they are consumed in order
    frame = 0
    frames = len(aligned)
    blank = 0
    # forced_align emits each target token over a run of frames, in order,
    # with blanks elsewhere. Walk frames and assign runs to tokens in order.
    while frame < frames and cursor < len(tokens):
        token_id = aligned[frame]
        if token_id == blank:
            frame += 1
            continue
        start = frame
        run_scores = []
        while frame < frames and aligned[frame] == token_id:
            run_scores.append(scores[frame])
            frame += 1
        # The run belongs to the next unconsumed token; forced_align
        # guarantees target order, so runs map 1:1 onto tokens.
        spans[cursor] = TokenSpan(
            token=tokens[cursor], start=start * ratio, end=frame * ratio,
            score=sum(run_scores) / len(run_scores))
        cursor += 1
    if cursor != len(tokens) or any(span is None for span in spans):
        raise AlignmentError("ALIGNMENT_FAILED")
    token_spans = [span for span in spans if span is not None]
    separator_spans = {i: span for i, span in enumerate(token_spans)
                       if span.token == "|"}
    char_losses: list[float | None] = []
    for plan in plans:
        if plan.kind == "token":
            char_losses.append(-token_spans[plan.token_index].score)
        else:
            char_losses.append(None)
    return token_spans, separator_spans, char_losses


def _decode_wav(wav_bytes: bytes):
    """Decode WAV bytes to (mono float32 waveform tensor, rate, seconds)."""
    import io

    try:
        import soundfile as sf
        import torch

        data, sample_rate = sf.read(io.BytesIO(wav_bytes), dtype="float32",
                                    always_2d=True)
        if data.shape[0] == 0 or sample_rate <= 0:
            raise AlignmentError("ALIGNMENT_AUDIO_INVALID")
        mono = data.mean(axis=1)
        waveform = torch.tensor(mono, dtype=torch.float32)
        duration = data.shape[0] / float(sample_rate)
        return waveform, int(sample_rate), duration
    except AlignmentError:
        raise
    except Exception as exc:
        raise AlignmentError("ALIGNMENT_AUDIO_INVALID") from exc
