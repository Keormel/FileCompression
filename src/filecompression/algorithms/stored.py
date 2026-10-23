from __future__ import annotations

from filecompression.errors import CorruptDataError
from .base import MAX_DECOMPRESSED_SIZE, CompressionAlgorithm, ProgressCallback


class StoredAlgorithm(CompressionAlgorithm):
    name = "stored"

    def compress(self, data: bytes, progress: ProgressCallback | None = None) -> tuple[bytes, bytes]:
        if progress is not None:
            progress(len(data), len(data))
        return data, b""

    def decompress(self, payload: bytes, metadata: bytes, limit: int = MAX_DECOMPRESSED_SIZE) -> bytes:
        if metadata or len(payload) > min(limit, MAX_DECOMPRESSED_SIZE):
            raise CorruptDataError("Invalid stored stream")
        return bytes(payload)
