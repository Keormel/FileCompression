from __future__ import annotations

import struct

from filecompression.errors import CorruptDataError
from .base import CompressionAlgorithm


class RleAlgorithm(CompressionAlgorithm):
    name = "rle"

    def compress(self, data: bytes) -> tuple[bytes, bytes]:
        output = bytearray()
        index = 0
        while index < len(data):
            value = data[index]
            end = index + 1
            while end < len(data) and data[end] == value and end - index < 0xFFFFFFFF:
                end += 1
            output.extend(struct.pack(">I", end - index))
            output.append(value)
            index = end
        return bytes(output), b""

    def decompress(self, payload: bytes, metadata: bytes) -> bytes:
        if metadata or len(payload) % 5:
            raise CorruptDataError("Invalid RLE stream")
        output = bytearray()
        for index in range(0, len(payload), 5):
            count = struct.unpack(">I", payload[index:index + 4])[0]
            if count == 0:
                raise CorruptDataError("RLE run cannot be empty")
            output.extend(bytes([payload[index + 4]]) * count)
        return bytes(output)
