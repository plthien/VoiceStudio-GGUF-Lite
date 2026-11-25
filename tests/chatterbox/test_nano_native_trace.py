"""Opt-in native inference diagnostics; no CUDA or real voice is required."""
from __future__ import annotations

import hashlib
import json
import wave

from lite.chatterbox_segmentation import segment_text
from lite.chatterbox_trace import record, wav_snapshot
from .test_contracts import api, gen, profile


def test_native_trace_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("NANO_INFERENCE_TRACE_DIR", raising=False)
    monkeypatch.delenv("NANO_INFERENCE_TRACE_SAVE_WAV", raising=False)
    record("request_received", trace_id="a" * 24, input_text="Private words.")
    assert wav_snapshot(b"RIFFfake", trace_id="a" * 24, chunk_index=1, stage="native") is None
    assert list(tmp_path.iterdir()) == []


def test_native_trace_exact_chunk_seed_and_raw_wav(api, tmp_path, monkeypatch):
    directory = tmp_path / "trace"
    monkeypatch.setenv("NANO_INFERENCE_TRACE_DIR", str(directory))
    monkeypatch.setenv("NANO_INFERENCE_TRACE_SAVE_WAV", "1")
    client, service, _ = api
    voice = profile(client)
    text = "Finally. A problem he knows how to solve."
    response = gen(client, voice["id"], text, speed=1.0)
    assert response.status_code == 200, response.text

    events = [
        json.loads(line)
        for line in (directory / "nano_native_trace.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [event["event"] for event in events] == [
        "request_received", "native_chunk_input",
        "native_chunk_output", "request_completed",
    ]
    assert len({event["trace_id"] for event in events}) == 1
    assert events[0]["input_text"] == text
    assert events[0]["request_seed"] == 42
    assert events[0]["speed"] == 1.0
    assert events[0]["input_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert events[1]["chunk_text"] == text
    assert events[1]["derived_seed"] == segment_text(text, 42)[0].seed
    assert events[1]["chunk_text_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    raw_file = directory / events[2]["raw_wav_filename"]
    assert raw_file.exists()
    assert hashlib.sha256(raw_file.read_bytes()).hexdigest() == events[2]["native_wav_sha256"]
    with wave.open(str(raw_file), "rb") as wav:
        assert wav.getframerate() == 24000
        assert wav.getnframes() > 0
    assert events[3]["output_wav_sha256"] == hashlib.sha256(response.content).hexdigest()
    assert (directory / events[3]["final_wav_filename"]).read_bytes() == response.content
    full_log = (directory / "nano_native_trace.jsonl").read_text(encoding="utf-8")
    assert "reference_sha" not in full_log
    assert "cancel_token" not in full_log


def test_native_trace_jsonl_only_without_audio_snapshot(api, tmp_path, monkeypatch):
    directory = tmp_path / "trace"
    monkeypatch.setenv("NANO_INFERENCE_TRACE_DIR", str(directory))
    monkeypatch.delenv("NANO_INFERENCE_TRACE_SAVE_WAV", raising=False)
    client, _, _ = api
    voice = profile(client)
    response = gen(client, voice["id"], "A short sentence.", speed=1.0)
    assert response.status_code == 200
    assert (directory / "nano_native_trace.jsonl").is_file()
    assert not list(directory.glob("*.wav"))


def test_native_trace_unwritable_directory_fails_open(tmp_path, monkeypatch):
    occupied = tmp_path / "occupied-file"
    occupied.write_text("file", encoding="utf-8")
    monkeypatch.setenv("NANO_INFERENCE_TRACE_DIR", str(occupied))
    monkeypatch.setenv("NANO_INFERENCE_TRACE_SAVE_WAV", "1")
    record("request_received", trace_id="c" * 24, input_text="Hello.")
    assert wav_snapshot(b"RIFFfake", trace_id="c" * 24, chunk_index=1, stage="native") is None
    assert occupied.read_text(encoding="utf-8") == "file"
