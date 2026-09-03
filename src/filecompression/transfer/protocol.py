from __future__ import annotations

import asyncio
import json
import struct

from filecompression.errors import CompressionError

MAX_FRAME_SIZE = 256 * 1024 * 1024


async def send_frame(writer: asyncio.StreamWriter, payload: bytes) -> None:
    if len(payload) > MAX_FRAME_SIZE:
        raise CompressionError("Frame is too large")
    writer.write(struct.pack(">I", len(payload)) + payload)
    await writer.drain()


async def receive_frame(reader: asyncio.StreamReader) -> bytes:
    header = await reader.readexactly(4)
    size = struct.unpack(">I", header)[0]
    if size > MAX_FRAME_SIZE:
        raise CompressionError("Frame is too large")
    return await reader.readexactly(size)


async def send_message(writer: asyncio.StreamWriter, message: dict) -> None:
    await send_frame(writer, json.dumps(message, separators=(",", ":")).encode("utf-8"))


async def receive_message(reader: asyncio.StreamReader) -> dict:
    try:
        message = json.loads((await receive_frame(reader)).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CompressionError("Invalid protocol message") from error
    if not isinstance(message, dict):
        raise CompressionError("Protocol message must be an object")
    return message
