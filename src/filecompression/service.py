from __future__ import annotations

import hashlib
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .container import iter_decompress, pack_stream, unpack_stream
from .parallel import shared_executor


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
    """Stream ``source`` into an FCMP container; memory use does not depend on the file size."""
    executor = shared_executor()
    started = time.perf_counter()
    with source.open("rb") as reader, _atomic_writer(destination) as writer:
        info = pack_stream(reader, writer, source.name, algorithm, executor=executor)
    compression_time = time.perf_counter() - started
    # Restore once without writing anything to confirm the archive and measure decoding speed.
    started = time.perf_counter()
    with destination.open("rb") as reader:
        for _ in iter_decompress(reader, executor=executor):
            pass
    decompression_time = time.perf_counter() - started
    return CompressionStats(info.original_size, info.compressed_size, compression_time, decompression_time)


def decompress_file(source: Path, destination: Path) -> None:
    with source.open("rb") as reader, _atomic_writer(destination) as writer:
        unpack_stream(reader, writer, executor=shared_executor())


class _atomic_writer:
    """Write to a temporary file next to ``destination`` and rename it into place on success."""

    def __init__(self, destination: Path) -> None:
        self.destination = destination

    def __enter__(self):
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        self.handle = tempfile.NamedTemporaryFile(prefix=f".{self.destination.name}.", dir=self.destination.parent, delete=False)
        return self.handle

    def __exit__(self, exc_type, exc, traceback) -> None:
        temporary = Path(self.handle.name)
        try:
            if exc_type is None:
                self.handle.flush()
                os.fsync(self.handle.fileno())
            self.handle.close()
            if exc_type is None:
                temporary.replace(self.destination)
        finally:
            if exc_type is not None:
                temporary.unlink(missing_ok=True)


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
