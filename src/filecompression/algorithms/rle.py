from __future__ import annotations

import re
import struct

from filecompression.errors import CorruptDataError
from .base import MAX_DECOMPRESSED_SIZE, CompressionAlgorithm, ProgressCallback

# Only runs of two or more bytes go through the regex loop; bytes between them are
# emitted as length-1 records in bulk, which keeps noisy data fast.
_RUN = re.compile(rb"(.)\1+", re.DOTALL)
_RECORD = struct.Struct(">IB")
_MAX_RUN = 0xFFFFFFFF
_SINGLE_BYTES = [bytes((value,)) for value in range(256)]
_SINGLE_RECORDS = [_RECORD.pack(1, value) for value in range(256)]


class RleAlgorithm(CompressionAlgorithm):
    name = "rle"

    def compress(self, data: bytes, progress: ProgressCallback | None = None) -> tuple[bytes, bytes]:
        output = bytearray()
        pack = _RECORD.pack
        position = 0
        for match in _RUN.finditer(data):
            start, end = match.span()
            if start > position:
                output += b"".join(map(_SINGLE_RECORDS.__getitem__, data[position:start]))
            position = end
            value = data[start]
            length = end - start
            while length > _MAX_RUN:
                output += pack(_MAX_RUN, value)
                length -= _MAX_RUN
            output += pack(length, value)
        if position < len(data):
            output += b"".join(map(_SINGLE_RECORDS.__getitem__, data[position:]))
        if progress is not None:
            progress(len(data), len(data))
        return bytes(output), b""

    def decompress(self, payload: bytes, metadata: bytes, limit: int = MAX_DECOMPRESSED_SIZE) -> bytes:
        if metadata or len(payload) % _RECORD.size:
            raise CorruptDataError("Invalid RLE stream")
        records = list(_RECORD.iter_unpack(payload))
        total = 0
        for count, _ in records:
            if count == 0:
                raise CorruptDataError("RLE run cannot be empty")
            total += count
        if total > min(limit, MAX_DECOMPRESSED_SIZE):
            raise CorruptDataError("RLE output exceeds the safety limit")
        return b"".join(_SINGLE_BYTES[value] * count for count, value in records)
