"""FCMP container.

Version 2 (written by this module) is a block stream, so files of any size are
compressed and restored with bounded memory:

    header   >4sBBHQHI  magic, version, algorithm, flags, original size, name length, block size
    name     UTF-8 filename
    blocks   >BIII      algorithm, original length, metadata length, payload length; then metadata, payload
    end      >BIII      all zero
    trailer  32 bytes   SHA-256 of the original data

Each block is compressed independently and falls back to ``stored`` when the codec
does not make it smaller. Version 1 (single block, checksum in the header) is still
readable.
"""

from __future__ import annotations

import hashlib
import io
import math
import os
import struct
from collections import deque
from collections.abc import Callable, Iterator
from concurrent.futures import Executor
from dataclasses import dataclass, field
from typing import BinaryIO

from filecompression.algorithms import get_algorithm
from filecompression.algorithms._accel import histogram
from filecompression.algorithms.base import MAX_DECOMPRESSED_SIZE, ProgressCallback
from filecompression.errors import ContainerError, CorruptDataError, OperationCancelled
from filecompression.parallel import pipeline_depth

MAGIC = b"FCMP"
VERSION = 2
_HEADER_V1 = struct.Struct(">4sBBHQH32sIQ")
_HEADER = struct.Struct(">4sBBHQHI")
_BLOCK = struct.Struct(">BIII")
_PREFIX = struct.Struct(">4sB")
HEADER_SIZE = _HEADER.size
_ALGORITHM_IDS = {"huffman": 1, "lzw": 2, "rle": 3, "stored": 4}
_ID_TO_ALGORITHM = {value: key for key, value in _ALGORITHM_IDS.items()}
_STORED_ID = _ALGORITHM_IDS["stored"]
_MAX_NAME_BYTES = 1024
_MAX_METADATA_BYTES = 16 * 1024 * 1024
_MAX_PAYLOAD_BYTES = 256 * 1024 * 1024
DEFAULT_BLOCK_SIZE = 1024 * 1024
MAX_BLOCK_SIZE = 64 * 1024 * 1024
MAX_ORIGINAL_SIZE = 1 << 40
_PROBE_SIZE = 64 * 1024
_ENTROPY_STRIDE = 16
_HIGH_ENTROPY_BITS = 7.5
CancelCheck = Callable[[], bool]


def validate_filename(filename: str) -> str:
    if not isinstance(filename, str):
        raise ContainerError("Filename must be a string")
    safe_name = os.path.basename(filename)
    if "/" in filename or "\\" in filename or not safe_name or safe_name in (".", "..") or safe_name != filename or any(ord(character) < 32 or ord(character) == 127 for character in safe_name):
        raise ContainerError("Filename must not contain directory components or control characters")
    if len(safe_name.encode("utf-8")) > _MAX_NAME_BYTES:
        raise ContainerError("Filename is too long")
    return safe_name


@dataclass(frozen=True)
class ContainerInfo:
    filename: str
    algorithm: str
    original_size: int
    checksum: str
    compressed_size: int
    payload_size: int
    version: int
    block_count: int


@dataclass(frozen=True)
class Container(ContainerInfo):
    raw: bytes = field(default=b"", repr=False)

    def decompress(self) -> bytes:
        return b"".join(iter_decompress(io.BytesIO(self.raw)))


# ---------------------------------------------------------------- block codecs
# Module-level functions so they can run inside the shared process pool.

def _looks_incompressible(algorithm: str, chunk: bytes) -> bool:
    """Cheap pre-check so already-compressed blocks (JPEG, ZIP, video) skip the full codec run.

    Only blocks whose sampled byte entropy is close to 8 bits are probed: the codec runs
    on the first 64 KiB, and the block is stored if even that does not shrink.
    """
    if len(chunk) <= _PROBE_SIZE:
        return False
    sample = chunk[::_ENTROPY_STRIDE]
    entropy = -sum(count * math.log2(count / len(sample)) for count in histogram(sample) if count) / len(sample)
    if entropy < _HIGH_ENTROPY_BITS:
        return False
    probe = chunk[:_PROBE_SIZE]
    payload, metadata = get_algorithm(algorithm).compress(probe)
    # Metadata (the Huffman frequency table) is spread over the whole block in the real run.
    return len(payload) + len(metadata) * len(probe) // len(chunk) >= len(probe)


def _encode_block(algorithm: str, chunk: bytes) -> tuple[int, bytes, bytes | None]:
    """Return (algorithm id, metadata, payload); payload ``None`` means "store the chunk as is"."""
    if algorithm != "stored" and not _looks_incompressible(algorithm, chunk):
        payload, metadata = get_algorithm(algorithm).compress(chunk)
        if len(payload) + len(metadata) < len(chunk):
            return _ALGORITHM_IDS[algorithm], metadata, payload
    return _STORED_ID, b"", None


def _decode_block(algorithm_id: int, original_length: int, metadata: bytes, payload: bytes) -> bytes:
    restored = get_algorithm(_ID_TO_ALGORITHM[algorithm_id]).decompress(payload, metadata, original_length)
    if len(restored) != original_length:
        raise CorruptDataError("Block size does not match container metadata")
    return restored


def _ordered(executor: Executor | None, function, jobs: Iterator[tuple]) -> Iterator[tuple[tuple, object]]:
    """Run ``function(*job)`` for each job, keeping a bounded number in flight, yielding results in order."""
    if executor is None:
        for job in jobs:
            yield job, function(*job)
        return
    depth = pipeline_depth(executor)
    pending: deque = deque()
    try:
        for job in jobs:
            pending.append((job, executor.submit(function, *job)))
            if len(pending) >= depth:
                job, future = pending.popleft()
                yield job, future.result()
        while pending:
            job, future = pending.popleft()
            yield job, future.result()
    finally:
        for _, future in pending:
            future.cancel()


# ---------------------------------------------------------------- writing

def pack_stream(
    source: BinaryIO,
    destination: BinaryIO,
    filename: str,
    algorithm: str,
    *,
    progress: ProgressCallback | None = None,
    cancelled: CancelCheck | None = None,
    block_size: int = DEFAULT_BLOCK_SIZE,
    executor: Executor | None = None,
) -> ContainerInfo:
    """Compress everything from ``source`` (seekable) into ``destination`` (seekable)."""
    safe_name = validate_filename(filename)
    algorithm_name = algorithm.lower()
    if algorithm_name not in _ALGORITHM_IDS:
        raise ContainerError(f"Unknown algorithm: {algorithm}")
    if not 0 < block_size <= MAX_BLOCK_SIZE:
        raise ContainerError("Invalid block size")
    start = source.tell()
    total = source.seek(0, io.SEEK_END) - start
    source.seek(start)
    if total > MAX_ORIGINAL_SIZE:
        raise ContainerError("Input data exceeds the safety limit")

    name_bytes = safe_name.encode("utf-8")
    header_offset = destination.tell()
    destination.write(_HEADER.pack(MAGIC, VERSION, _STORED_ID, 0, total, len(name_bytes), block_size))
    destination.write(name_bytes)
    written = _HEADER.size + len(name_bytes)
    digest = hashlib.sha256()
    processed = payload_size = blocks = 0
    used_algorithm = False

    def chunks() -> Iterator[tuple[str, bytes]]:
        while True:
            if cancelled is not None and cancelled():
                raise OperationCancelled("Compression cancelled")
            chunk = source.read(block_size)
            if not chunk:
                return
            digest.update(chunk)
            yield algorithm_name, chunk

    for (_, chunk), (algorithm_id, metadata, payload) in _ordered(executor, _encode_block, chunks()):
        if payload is None:
            payload = chunk
        destination.write(_BLOCK.pack(algorithm_id, len(chunk), len(metadata), len(payload)))
        destination.write(metadata)
        destination.write(payload)
        written += _BLOCK.size + len(metadata) + len(payload)
        payload_size += len(metadata) + len(payload)
        processed += len(chunk)
        blocks += 1
        used_algorithm = used_algorithm or algorithm_id != _STORED_ID
        if progress is not None:
            progress(processed, total)
    if processed != total:
        raise ContainerError("Input changed while it was being compressed")
    destination.write(_BLOCK.pack(0, 0, 0, 0))
    destination.write(digest.digest())
    written += _BLOCK.size + digest.digest_size
    final_algorithm = algorithm_name if used_algorithm else "stored"
    end = destination.tell()
    destination.seek(header_offset + _PREFIX.size)
    destination.write(bytes((_ALGORITHM_IDS[final_algorithm],)))
    destination.seek(end)
    if progress is not None and total == 0:
        progress(0, 0)
    return ContainerInfo(safe_name, final_algorithm, total, digest.hexdigest(), written, payload_size, VERSION, blocks)


def pack(data: bytes, filename: str, algorithm: str, progress: ProgressCallback | None = None) -> bytes:
    """In-memory convenience wrapper around :func:`pack_stream` for small inputs."""
    if not isinstance(data, bytes) or len(data) > MAX_DECOMPRESSED_SIZE:
        raise ContainerError("Input data exceeds the safety limit")
    if not isinstance(filename, str) or not isinstance(algorithm, str):
        raise ContainerError("Filename and algorithm must be strings")
    output = io.BytesIO()
    pack_stream(io.BytesIO(data), output, filename, algorithm, progress=progress)
    return output.getvalue()


# ---------------------------------------------------------------- reading

def _read_exact(source: BinaryIO, size: int) -> bytes:
    data = source.read(size)
    if len(data) != size:
        raise ContainerError("Container is truncated")
    return data


def _read_name(source: BinaryIO, length: int) -> str:
    try:
        filename = _read_exact(source, length).decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContainerError("Filename is not valid UTF-8") from error
    return validate_filename(filename)


def _read_prefix(source: BinaryIO) -> int:
    magic, version = _PREFIX.unpack(_read_exact(source, _PREFIX.size))
    if magic != MAGIC or version not in (1, VERSION):
        raise ContainerError("Unsupported container format")
    return version


def _read_v1_header(source: BinaryIO, start: int) -> tuple[str, str, int, bytes, int, int]:
    source.seek(start)
    magic, version, algorithm_id, flags, original_size, name_length, checksum, metadata_length, payload_length = _HEADER_V1.unpack(_read_exact(source, _HEADER_V1.size))
    algorithm = _ID_TO_ALGORITHM.get(algorithm_id)
    if flags != 0 or algorithm is None or original_size > MAX_DECOMPRESSED_SIZE or name_length > _MAX_NAME_BYTES or metadata_length > _MAX_METADATA_BYTES or payload_length > _MAX_PAYLOAD_BYTES:
        raise ContainerError("Invalid container metadata")
    filename = _read_name(source, name_length)
    return filename, algorithm, original_size, checksum, metadata_length, payload_length


def _read_v2_header(source: BinaryIO, start: int) -> tuple[str, str, int, int]:
    source.seek(start)
    magic, version, algorithm_id, flags, original_size, name_length, block_size = _HEADER.unpack(_read_exact(source, _HEADER.size))
    algorithm = _ID_TO_ALGORITHM.get(algorithm_id)
    if flags != 0 or algorithm is None or original_size > MAX_ORIGINAL_SIZE or name_length > _MAX_NAME_BYTES or not 0 < block_size <= MAX_BLOCK_SIZE:
        raise ContainerError("Invalid container metadata")
    filename = _read_name(source, name_length)
    return filename, algorithm, original_size, block_size


def _read_block_header(source: BinaryIO, block_size: int, remaining: int) -> tuple[int, int, int, int]:
    algorithm_id, original_length, metadata_length, payload_length = _BLOCK.unpack(_read_exact(source, _BLOCK.size))
    if algorithm_id == 0:
        if original_length or metadata_length or payload_length or remaining:
            raise ContainerError("Invalid end of block stream")
        return 0, 0, 0, 0
    if algorithm_id not in _ID_TO_ALGORITHM or not 0 < original_length <= min(block_size, remaining):
        raise ContainerError("Invalid block header")
    if algorithm_id == _STORED_ID:
        if metadata_length or payload_length != original_length:
            raise ContainerError("Invalid stored block")
    elif metadata_length + payload_length >= original_length:
        raise ContainerError("Invalid compressed block")
    return algorithm_id, original_length, metadata_length, payload_length


def read_info(source: BinaryIO) -> ContainerInfo:
    """Validate the container structure without decompressing it (reads headers only)."""
    start = source.tell()
    end = source.seek(0, io.SEEK_END)
    if end - start < _HEADER.size:
        raise ContainerError("Container is truncated")
    source.seek(start)
    version = _read_prefix(source)
    if version == 1:
        filename, algorithm, original_size, checksum, metadata_length, payload_length = _read_v1_header(source, start)
        header_end = source.tell()
        if header_end + metadata_length + payload_length != end:
            raise ContainerError("Container length does not match its header")
        return ContainerInfo(filename, algorithm, original_size, checksum.hex(), end - start, metadata_length + payload_length, 1, 1)

    filename, algorithm, original_size, block_size = _read_v2_header(source, start)
    remaining = original_size
    payload_size = blocks = 0
    while True:
        algorithm_id, original_length, metadata_length, payload_length = _read_block_header(source, block_size, remaining)
        if algorithm_id == 0:
            break
        position = source.seek(metadata_length + payload_length, io.SEEK_CUR)
        if position > end:
            raise ContainerError("Container is truncated")
        remaining -= original_length
        payload_size += metadata_length + payload_length
        blocks += 1
    checksum = _read_exact(source, hashlib.sha256().digest_size)
    if source.tell() != end:
        raise ContainerError("Container length does not match its header")
    return ContainerInfo(filename, algorithm, original_size, checksum.hex(), end - start, payload_size, VERSION, blocks)


def read_info_path(path: str | os.PathLike) -> ContainerInfo:
    with open(path, "rb") as handle:
        return read_info(handle)


def iter_decompress(source: BinaryIO, *, executor: Executor | None = None, cancelled: CancelCheck | None = None) -> Iterator[bytes]:
    """Yield the restored data block by block; the SHA-256 is checked before the final block is released."""
    start = source.tell()
    version = _read_prefix(source)
    if version == 1:
        filename, algorithm, original_size, checksum, metadata_length, payload_length = _read_v1_header(source, start)
        metadata = _read_exact(source, metadata_length)
        payload = _read_exact(source, payload_length)
        if source.read(1):
            raise ContainerError("Container length does not match its header")
        restored = get_algorithm(algorithm).decompress(payload, metadata, original_size)
        if len(restored) != original_size:
            raise CorruptDataError("Restored size does not match container metadata")
        if hashlib.sha256(restored).digest() != checksum:
            raise CorruptDataError("Checksum mismatch")
        yield restored
        return

    _, _, original_size, block_size = _read_v2_header(source, start)
    digest = hashlib.sha256()

    def blocks() -> Iterator[tuple[int, int, bytes, bytes]]:
        remaining = original_size
        while True:
            if cancelled is not None and cancelled():
                raise OperationCancelled("Decompression cancelled")
            algorithm_id, original_length, metadata_length, payload_length = _read_block_header(source, block_size, remaining)
            if algorithm_id == 0:
                return
            remaining -= original_length
            yield algorithm_id, original_length, _read_exact(source, metadata_length), _read_exact(source, payload_length)

    held: bytes | None = None
    for _, restored in _ordered(executor, _decode_block, blocks()):
        digest.update(restored)
        if held is not None:
            yield held
        held = restored
    checksum = _read_exact(source, digest.digest_size)
    if source.read(1):
        raise ContainerError("Container length does not match its header")
    if digest.digest() != checksum:
        raise CorruptDataError("Checksum mismatch")
    if held is not None:
        yield held


def unpack_stream(source: BinaryIO, destination: BinaryIO, *, executor: Executor | None = None, cancelled: CancelCheck | None = None) -> int:
    """Restore a container into ``destination``; returns the number of bytes written."""
    written = 0
    for chunk in iter_decompress(source, executor=executor, cancelled=cancelled):
        destination.write(chunk)
        written += len(chunk)
    return written


def unpack(raw: bytes) -> Container:
    """Parse an in-memory container; call ``.decompress()`` to restore the data."""
    info = read_info(io.BytesIO(raw))
    return Container(**info.__dict__, raw=raw)
