"""Bounded, resumable long-form benchmark helpers (no private content logging)."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import struct
import tempfile
import time
import wave
from dataclasses import asdict, dataclass
from pathlib import Path

from .pause import (COMPLETE, MARKER_START, MAX_MARKERS, MAX_TOTAL_MS,
                    PausePart, parse_pause_script)

MAX_CHARS = 4096
MAX_PAUSES = MAX_MARKERS
MAX_ESTIMATED_SECONDS = 60.0
MAX_WAV_FRAMES = 900 * 24000  # Explicit validation bound; owner 900-s stability fixture.
PLAIN_UNIT_RE = re.compile(r"\s+|\S+")


@dataclass(frozen=True)
class Section:
    index: int
    text: str
    seed: int

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


def _pause_markers(text: str) -> list[tuple[int, int, int]]:
    """Return canonical marker spans/durations without aggregate limits."""
    markers: list[tuple[int, int, int]] = []
    end = 0
    for start in MARKER_START.finditer(text):
        if start.start() < end:
            continue
        complete = COMPLETE.match(text, start.start())
        if complete is None:
            parse_pause_script("x " + text[start.start():] + " y")
            raise AssertionError("canonical pause parser accepted an incomplete marker")
        raw = text[start.start():complete.end()]
        parsed = parse_pause_script("x " + raw + " y")
        pause = next(part for part in parsed.parts if isinstance(part, PausePart))
        markers.append((start.start(), complete.end(), pause.milliseconds))
        end = complete.end()
    return markers


def pause_count(text: str) -> int:
    return len(_pause_markers(text))


def _units(text: str) -> list[str]:
    """Tokenize losslessly without cutting inside a canonical marker."""
    result: list[str] = []
    end = 0
    for start, stop, _milliseconds in _pause_markers(text):
        result.extend(PLAIN_UNIT_RE.findall(text[end:start]))
        result.append(text[start:stop])
        end = stop
    result.extend(PLAIN_UNIT_RE.findall(text[end:]))
    return result


def estimated_seconds(text: str, words_per_minute: float = 150.0) -> float:
    if words_per_minute <= 0:
        raise ValueError("words_per_minute must be positive")
    markers = _pause_markers(text)
    speech_parts: list[str] = []
    end = 0
    for start, stop, _milliseconds in markers:
        speech_parts.append(text[end:start])
        end = stop
    speech_parts.append(text[end:])
    speech = len(re.findall(r"\S+", "".join(speech_parts))) * 60.0 / words_per_minute
    return speech + sum(marker[2] for marker in markers) / 1000.0


def _valid(text: str, words_per_minute: float) -> bool:
    markers = _pause_markers(text)
    return (len(text) <= MAX_CHARS and len(markers) <= MAX_PAUSES and
            sum(marker[2] for marker in markers) <= MAX_TOTAL_MS and
            estimated_seconds(text, words_per_minute) <= MAX_ESTIMATED_SECONDS)


def split_section(text: str, *, seed: int = 42,
                  words_per_minute: float = 150.0) -> list[Section]:
    """Split only at safe units, preserving every character exactly."""
    if not text or not text.strip():
        raise ValueError("section text is empty")
    _pause_markers(text)
    if _valid(text, words_per_minute):
        return [Section(0, text, seed)]
    pieces = re.findall(r".*?(?:[.!?。！？](?:\s+|$)|$)", text, re.S)
    pieces = [piece for piece in pieces if piece]
    if len(pieces) <= 1:
        pieces = _units(text)
    result: list[str] = []
    current = ""
    for piece in pieces:
        candidate = current + piece
        if current and not _valid(candidate, words_per_minute):
            result.append(current)
            current = piece
        else:
            current = candidate
        if not _valid(current, words_per_minute):
            oversized = current
            current = ""
            for unit in _units(oversized):
                candidate = current + unit
                if current and not _valid(candidate, words_per_minute):
                    result.append(current)
                    current = unit
                else:
                    current = candidate
                if not _valid(current, words_per_minute):
                    raise ValueError("a single token exceeds a section bound")
    if current:
        result.append(current)
    if "".join(result) != text or not all(_valid(x, words_per_minute) for x in result):
        raise AssertionError("lossless bounded split invariant failed")
    return [Section(i, value, (seed + i) % 2147483648)
            for i, value in enumerate(result)]


def create_manifest(texts: list[str], *, voice_version: int,
                    profile_binding_sha256: str, seed: int = 42,
                    runtime_binding_sha256: str | None = None,
                    speed: float = 1.0,
                    words_per_minute: float = 150.0) -> dict:
    if voice_version < 1:
        raise ValueError("voice_version must be positive")
    if not re.fullmatch(r"[0-9a-f]{64}", profile_binding_sha256):
        raise ValueError("profile binding must be a lowercase SHA-256 digest")
    if runtime_binding_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", runtime_binding_sha256):
        raise ValueError("runtime binding must be a SHA-256 digest")
    sections = []
    for authoritative_index, text in enumerate(texts):
        parts = split_section(
            text, seed=(seed + authoritative_index * 1000) % 2147483648,
            words_per_minute=words_per_minute)
        for part in parts:
            item = asdict(part)
            item.update(authoritative_index=authoritative_index,
                        text_sha256=part.sha256, chars=len(part.text),
                        pauses=pause_count(part.text),
                        pause_ms=sum(m[2] for m in _pause_markers(part.text)))
            sections.append(item)
    for index, item in enumerate(sections):
        item["index"] = index
    core = {"schema": "lite-longform-manifest-v2",
            "voice_version": voice_version,
            "profile_binding_sha256": profile_binding_sha256,
            "runtime_binding_sha256": runtime_binding_sha256 or "legacy-unbound",
            "seed": seed, "speed": speed,
            "words_per_minute": words_per_minute, "sections": sections}
    encoded = json.dumps(core, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode()
    return {**core, "manifest_sha256": hashlib.sha256(encoded).hexdigest()}


def write_manifest_atomic(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8-sig"))
        if existing != manifest:
            raise ValueError("resume manifest differs from immutable benchmark input")
        return
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as out:
            json.dump(manifest, out, indent=2, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def inspect_wav(path: Path) -> dict:
    """Validate frames in bounded chunks, never materializing full long-form PCM."""
    size = path.stat().st_size
    if size > MAX_WAV_FRAMES * 2 + 65536:
        raise ValueError("section WAV exceeds validation byte bound")
    # The wave module tolerates trailing non-audio bytes. Refuse malformed,
    # truncated or appended chunks before validating frame data.
    with path.open("rb") as source:
        header = source.read(12)
        if (len(header) != 12 or header[:4] != b"RIFF" or
                header[8:] != b"WAVE" or
                struct.unpack("<I", header[4:8])[0] + 8 != size):
            raise ValueError("section WAV RIFF length/header mismatch")
        at, data_size = 12, None
        while at < size:
            source.seek(at)
            chunk = source.read(8)
            if len(chunk) != 8:
                raise ValueError("section WAV truncated chunk header")
            kind, length = chunk[:4], struct.unpack("<I", chunk[4:])[0]
            next_at = at + 8 + length + (length & 1)
            if next_at > size:
                raise ValueError("section WAV truncated chunk")
            if data_size is not None:
                raise ValueError("section WAV has unexpected trailing chunk")
            if kind == b"data":
                data_size = length
                if next_at != size:
                    raise ValueError("section WAV has trailing data")
            at = next_at
        if data_size is None:
            raise ValueError("section WAV data chunk missing")
    with wave.open(str(path), "rb") as src:
        if (src.getnchannels(), src.getsampwidth(), src.getframerate()) != (1, 2, 24000):
            raise ValueError("section WAV must be 24 kHz mono PCM16")
        if src.getcomptype() != "NONE":
            raise ValueError("section WAV must be uncompressed PCM")
        frames = src.getnframes()
        if frames <= 0 or frames > MAX_WAV_FRAMES:
            raise ValueError("section WAV exceeds frame bound")
        if data_size != frames * 2:
            raise ValueError("section WAV PCM data length mismatch")
        count = 0
        while count < frames:
            requested = min(65536, frames - count)
            block = src.readframes(requested)
            if len(block) != requested * 2:
                raise ValueError("section WAV is truncated")
            count += requested
        if src.readframes(1):
            raise ValueError("section WAV has unexpected audio frames")
    return {"frames": frames, "audio_s": frames / 24000.0}


def speech_seconds(headers: dict, audio_s: float) -> float:
    """Resolve speech duration; no-pause responses omit the speech header."""
    if audio_s <= 0:
        raise ValueError("audio duration must be positive")
    normalized = {str(key).lower(): value for key, value in headers.items()}
    raw = normalized.get("x-lite-speech-s")
    value = audio_s if raw is None or str(raw).strip() == "" else float(raw)
    if value <= 0 or value > audio_s + (1 / 24000):
        raise ValueError("invalid speech duration")
    return value


def assemble_wavs(paths: list[Path], output: Path) -> dict:
    """Stream PCM between files; memory stays bounded by a 64 Ki-frame chunk."""
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(output.name + ".partial")
    frames = 0
    try:
        with wave.open(str(temp), "wb") as dst:
            dst.setnchannels(1)
            dst.setsampwidth(2)
            dst.setframerate(24000)
            for path in paths:
                inspect_wav(path)
                with wave.open(str(path), "rb") as src:
                    while True:
                        block = src.readframes(65536)
                        if not block:
                            break
                        dst.writeframesraw(block)
                        frames += len(block) // 2
        os.replace(temp, output)
    finally:
        if temp.exists():
            temp.unlink()
    return {"frames": frames, "audio_s": frames / 24000.0,
            "bytes": output.stat().st_size}


def metrics_row(*, index: int, wall_s: float, audio_s: float,
                speech_s: float, cold: bool, headers: dict) -> dict:
    if min(wall_s, audio_s, speech_s) <= 0:
        raise ValueError("metric durations must be positive")
    normalized = {str(k).lower(): str(v) for k, v in headers.items()}
    if normalized.get("x-lite-verification-status") != "verified":
        raise ValueError("unverified native response")
    return {"index": index, "wall_s": wall_s, "audio_s": audio_s,
            "speech_s": speech_s, "final_rtf": wall_s / audio_s,
            "speech_rtf": wall_s / speech_s, "cold": bool(cold),
            "verification_status": "verified"}


def write_metrics(directory: Path, rows: list[dict], started: float) -> dict:
    total_wall = time.monotonic() - started
    audio = sum(float(x["audio_s"]) for x in rows if x.get("audio_s") is not None)
    speech = sum(float(x["speech_s"]) for x in rows if x.get("speech_s") is not None)
    report = {"schema": "lite-longform-metrics-v1", "sections": rows,
              "total_wall_s": total_wall, "audio_s": audio,
              "speech_s": speech,
              "final_rtf": total_wall / audio if audio else None,
              "speech_rtf": total_wall / speech if speech else None,
              "privacy": "text, voice ids, reference bytes and audio paths omitted"}
    directory.mkdir(parents=True, exist_ok=True)
    # Both report files are durable independently after each completed outcome.
    def publish(path: Path, render):
        fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as out:
                render(out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    publish(directory / "metrics.json", lambda out: json.dump(report, out, indent=2))
    fieldnames = list(dict.fromkeys(k for row in rows for k in row)) or ["index"]
    def render_csv(out):
        writer = csv.DictWriter(out, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    publish(directory / "metrics.csv", render_csv)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Bounded long-form benchmark artifact helper")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--sections-file", type=Path, required=True,
                         help="local UTF-8 JSON array of authoritative section strings")
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--voice-version", type=int, required=True)
    prepare.add_argument("--profile-binding-sha256", required=True)
    prepare.add_argument("--runtime-binding-sha256")
    prepare.add_argument("--seed", type=int, default=42)
    prepare.add_argument("--speed", type=float, default=1.0)
    assemble = sub.add_parser("assemble")
    assemble.add_argument("--output", type=Path, required=True)
    assemble.add_argument("wav", type=Path, nargs="+")
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--wav", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        texts = json.loads(args.sections_file.read_text(encoding="utf-8-sig"))
        if not isinstance(texts, list) or not all(isinstance(x, str) for x in texts):
            raise ValueError("sections file must be a JSON string array")
        manifest = create_manifest(
            texts, voice_version=args.voice_version,
            profile_binding_sha256=args.profile_binding_sha256,
            runtime_binding_sha256=args.runtime_binding_sha256,
            seed=args.seed, speed=args.speed)
        write_manifest_atomic(args.output, manifest)
        print(json.dumps({"status": "PASS", "sections": len(manifest["sections"]),
                          "manifest_sha256": manifest["manifest_sha256"]}))
    elif args.command == "assemble":
        print(json.dumps({"status": "PASS", **assemble_wavs(args.wav, args.output)}))
    else:
        print(json.dumps({"status": "PASS", **inspect_wav(args.wav)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
