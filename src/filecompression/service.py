from __future__ import annotations

import hashlib
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .container import pack, unpack


@dataclass(frozen=True)
class CompressionStats:
    original_size: int
    compressed_size: int
    compression_time: float
    decompression_time: float

    @property
    def ratio(self) -> float:
        return self.original_size / self.compressed_size if self.compressed_size else 0.0

    @property
    def saving_percent(self) -> float:
        return (1 - self.compressed_size / self.original_size) * 100 if self.original_size else 0.0


def compress_file(source: Path, destination: Path, algorithm: str) -> CompressionStats:
    from .algorithms.base import MAX_DECOMPRESSED_SIZE
    if source.stat().st_size > MAX_DECOMPRESSED_SIZE:
        raise ValueError("Input file exceeds the 256 MiB safety limit")
    data = source.read_bytes()
    started = time.perf_counter()
    container = pack(data, source.name, algorithm)
    compression_time = time.perf_counter() - started
    _atomic_write(destination, container)
    started = time.perf_counter()
    unpack(container).decompress()
    decompression_time = time.perf_counter() - started
    return CompressionStats(len(data), len(container), compression_time, decompression_time)


def decompress_file(source: Path, destination: Path) -> None:
    _atomic_write(destination, unpack(source.read_bytes()).decompress())


def _atomic_write(destination: Path, data: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(prefix=f".{destination.name}.", dir=destination.parent, delete=False)
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
