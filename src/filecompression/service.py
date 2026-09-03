from __future__ import annotations

import hashlib
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
    data = source.read_bytes()
    started = time.perf_counter()
    container = pack(data, source.name, algorithm)
    compression_time = time.perf_counter() - started
    destination.write_bytes(container)
    started = time.perf_counter()
    unpack(container).decompress()
    decompression_time = time.perf_counter() - started
    return CompressionStats(len(data), len(container), compression_time, decompression_time)


def decompress_file(source: Path, destination: Path) -> None:
    destination.write_bytes(unpack(source.read_bytes()).decompress())


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
