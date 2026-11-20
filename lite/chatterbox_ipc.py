"""Bounded versioned binary frames. stdout is protocol-only, never private narration logs."""
from __future__ import annotations

import asyncio
import json
import struct
import sys

PROTOCOL = 1
MAX_FRAME = 120 * 1024 * 1024

class ProtocolError(RuntimeError):
    pass


def pack(obj: dict) -> bytes:
    wire = json.dumps({"v": PROTOCOL, **obj}, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(wire) > MAX_FRAME:
        raise ProtocolError("FRAME_TOO_LARGE")
    return struct.pack(">I", len(wire)) + wire


def unpack(raw: bytes) -> dict:
    if len(raw) > MAX_FRAME:
        raise ProtocolError("FRAME_TOO_LARGE")
    try:
        obj = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError("INVALID_FRAME") from exc
    if not isinstance(obj, dict) or obj.get("v") != PROTOCOL:
        raise ProtocolError("PROTOCOL_VERSION")
    return obj


async def recv(reader: asyncio.StreamReader) -> dict:
    n = struct.unpack(">I", await reader.readexactly(4))[0]
    if n == 0 or n > MAX_FRAME:
        raise ProtocolError("FRAME_TOO_LARGE")
    return unpack(await reader.readexactly(n))


async def send(writer: asyncio.StreamWriter, obj: dict) -> None:
    writer.write(pack(obj))
    await writer.drain()


def read_sync() -> dict | None:
    b = sys.stdin.buffer.read(4)
    if not b:
        return None
    if len(b) != 4:
        raise ProtocolError("TRUNCATED_FRAME")
    n = struct.unpack(">I", b)[0]
    if n == 0 or n > MAX_FRAME:
        raise ProtocolError("FRAME_TOO_LARGE")
    raw = sys.stdin.buffer.read(n)
    if len(raw) != n:
        raise ProtocolError("TRUNCATED_FRAME")
    return unpack(raw)


def write_sync(obj: dict) -> None:
    sys.stdout.buffer.write(pack(obj))
    sys.stdout.buffer.flush()
