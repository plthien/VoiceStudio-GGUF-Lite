"""Bounded, resumable long-form benchmark helpers (no private content logging)."""
from __future__ import annotations

import csv
import argparse
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

MAX_CHARS = 4096
MAX_PAUSES = 16
MAX_ESTIMATED_SECONDS = 60.0
PAUSE_RE = re.compile(r"\[pause\s+(?:\d+(?:\.\d+)?(?:ms|s))\]", re.I)
UNIT_RE = re.compile(r"\[pause\s+(?:\d+(?:\.\d+)?(?:ms|s))\]|\s+|\S+", re.I)
SENTENCE_RE = re.compile(r"(?<=[.!?。！？])\s+")


@dataclass(frozen=True)
class Section:
    index: int
    text: str
    seed: int

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


def estimated_seconds(text: str, words_per_minute: float = 150.0) -> float:
    if words_per_minute <= 0:
        raise ValueError("words_per_minute must be positive")
    speech = len(re.findall(r"\S+", PAUSE_RE.sub("", text))) * 60.0 / words_per_minute
    pause = 0.0
    for token in PAUSE_RE.findall(text):
        value = re.search(r"(\d+(?:\.\d+)?)(ms|s)", token, re.I)
        pause += float(value.group(1)) / (1000 if value.group(2).lower() == "ms" else 1)
    return speech + pause


def _valid(text: str, words_per_minute: float) -> bool:
    return (len(text) <= MAX_CHARS and len(PAUSE_RE.findall(text)) <= MAX_PAUSES and
            estimated_seconds(text, words_per_minute) <= MAX_ESTIMATED_SECONDS)


def split_section(text: str, *, seed: int = 42, words_per_minute: float = 150.0) -> list[Section]:
    """Split only at whitespace, preserving every non-boundary character exactly."""
    if not text or not text.strip():
        raise ValueError("section text is empty")
    if _valid(text, words_per_minute):
        return [Section(0, text, seed)]
    # Keep separators attached so concatenating the result is byte-for-byte
    # identical. Prefer sentence units, then fall back to whitespace units.
    pieces = re.findall(r".*?(?:[.!?。！？](?:\s+|$)|$)", text, re.S)
    pieces = [piece for piece in pieces if piece]
    if len(pieces) <= 1:
        pieces = UNIT_RE.findall(text)
    result, current = [], ""
    for piece in pieces:
        candidate = current + piece
        if current and not _valid(candidate, words_per_minute):
            result.append(current)
            current = piece
        else:
            current = candidate
        if not _valid(current, words_per_minute):
            # A sentence can itself exceed a bound. Re-run it using exact
            # whitespace/non-whitespace units; never cut inside a word.
            oversized=current;current=""
            for unit in UNIT_RE.findall(oversized):
                candidate=current+unit
                if current and not _valid(candidate,words_per_minute):
                    result.append(current);current=unit
                else:current=candidate
                if not _valid(current,words_per_minute):
                    raise ValueError("a single token exceeds a section bound")
    if current:
        result.append(current)
    if "".join(result) != text or not all(_valid(x, words_per_minute) for x in result):
        raise AssertionError("lossless bounded split invariant failed")
    return [Section(i, value, (seed + i) % 2147483648) for i, value in enumerate(result)]


def create_manifest(texts: list[str], *, voice_version: int, seed: int = 42,
                    speed: float = 1.0, words_per_minute: float = 150.0) -> dict:
    sections = []
    for authoritative_index, text in enumerate(texts):
        for part in split_section(text, seed=(seed + authoritative_index * 1000) % 2147483648,
                                  words_per_minute=words_per_minute):
            item = asdict(part)
            item.update(authoritative_index=authoritative_index, text_sha256=part.sha256,
                        chars=len(part.text), pauses=len(PAUSE_RE.findall(part.text)))
            sections.append(item)
    for index, item in enumerate(sections):
        item["index"] = index
    core = {"schema": "lite-longform-manifest-v1", "voice_version": voice_version,
            "seed": seed, "speed": speed, "words_per_minute": words_per_minute,
            "sections": sections}
    encoded = json.dumps(core, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return {**core, "manifest_sha256": hashlib.sha256(encoded).hexdigest()}


def write_manifest_atomic(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing != manifest:
            raise ValueError("resume manifest differs from immutable benchmark input")
        return
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as out:
            json.dump(manifest, out, indent=2, ensure_ascii=False)
            out.flush(); os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


def inspect_wav(path: Path) -> dict:
    with wave.open(str(path), "rb") as src:
        if (src.getnchannels(), src.getsampwidth(), src.getframerate()) != (1, 2, 24000):
            raise ValueError("section WAV must be 24 kHz mono PCM16")
        frames = src.getnframes()
        if frames <= 0 or len(src.readframes(frames)) != frames * 2:
            raise ValueError("section WAV is truncated")
    return {"frames": frames, "audio_s": frames / 24000.0}


def assemble_wavs(paths: list[Path], output: Path) -> dict:
    """Stream PCM between files; memory stays bounded by a 64 Ki-frame chunk."""
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(output.name + ".partial")
    frames = 0
    try:
        with wave.open(str(temp), "wb") as dst:
            dst.setnchannels(1); dst.setsampwidth(2); dst.setframerate(24000)
            for path in paths:
                inspect_wav(path)
                with wave.open(str(path), "rb") as src:
                    while True:
                        block = src.readframes(65536)
                        if not block: break
                        dst.writeframesraw(block); frames += len(block) // 2
        os.replace(temp, output)
    finally:
        if temp.exists(): temp.unlink()
    return {"frames": frames, "audio_s": frames / 24000.0, "bytes": output.stat().st_size}


def metrics_row(*, index: int, wall_s: float, audio_s: float, speech_s: float,
                cold: bool, headers: dict) -> dict:
    if min(wall_s, audio_s, speech_s) <= 0:
        raise ValueError("metric durations must be positive")
    normalized = {str(k).lower(): str(v) for k, v in headers.items()}
    if normalized.get("x-lite-verification-status") != "verified":
        raise ValueError("unverified native response")
    return {"index": index, "wall_s": wall_s, "audio_s": audio_s, "speech_s": speech_s,
            "final_rtf": wall_s / audio_s, "speech_rtf": wall_s / speech_s,
            "cold": bool(cold), "verification_status": "verified"}


def write_metrics(directory: Path, rows: list[dict], started: float) -> dict:
    total_wall = time.monotonic() - started
    audio = sum(float(x["audio_s"]) for x in rows)
    speech = sum(float(x["speech_s"]) for x in rows)
    report = {"schema": "lite-longform-metrics-v1", "sections": rows,
              "total_wall_s": total_wall, "audio_s": audio, "speech_s": speech,
              "final_rtf": total_wall / audio if audio else None,
              "speech_rtf": total_wall / speech if speech else None,
              "privacy": "text, voice ids, reference bytes and audio paths omitted"}
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "metrics.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    with (directory / "metrics.csv").open("w", encoding="utf-8", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=list(rows[0]) if rows else ["index"])
        writer.writeheader(); writer.writerows(rows)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Bounded long-form benchmark artifact helper")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--sections-file", type=Path, required=True,
                         help="local UTF-8 JSON array of authoritative section strings")
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--voice-version", type=int, required=True)
    prepare.add_argument("--seed", type=int, default=42)
    prepare.add_argument("--speed", type=float, default=1.0)
    assemble = sub.add_parser("assemble")
    assemble.add_argument("--output", type=Path, required=True)
    assemble.add_argument("wav", type=Path, nargs="+")
    args = parser.parse_args(argv)
    if args.command == "prepare":
        texts = json.loads(args.sections_file.read_text(encoding="utf-8"))
        if not isinstance(texts, list) or not all(isinstance(x, str) for x in texts):
            raise ValueError("sections file must be a JSON string array")
        manifest = create_manifest(texts, voice_version=args.voice_version,
                                   seed=args.seed, speed=args.speed)
        write_manifest_atomic(args.output, manifest)
        print(json.dumps({"status": "PASS", "sections": len(manifest["sections"]),
                          "manifest_sha256": manifest["manifest_sha256"]}))
    else:
        print(json.dumps({"status": "PASS", **assemble_wavs(args.wav, args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
