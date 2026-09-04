from __future__ import annotations

import hashlib
import os
import struct
from dataclasses import dataclass

from filecompression.algorithms import get_algorithm
from filecompression.algorithms.base import MAX_DECOMPRESSED_SIZE, ProgressCallback
from filecompression.errors import ContainerError, CorruptDataError

MAGIC = b"FCMP"
VERSION = 1
_HEADER = struct.Struct(">4sBBHQH32sIQ")
_ALGORITHM_IDS = {"huffman": 1, "lzw": 2, "rle": 3, "stored": 4}
_ID_TO_ALGORITHM = {value: key for key, value in _ALGORITHM_IDS.items()}
_MAX_NAME_BYTES = 1024
_MAX_METADATA_BYTES = 16 * 1024 * 1024
_MAX_PAYLOAD_BYTES = 256 * 1024 * 1024


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
class Container:
    filename: str
    algorithm: str
    original_size: int
    checksum: str
    metadata: bytes
    payload: bytes

    @property
    def compressed_size(self) -> int:
        return _HEADER.size + len(self.filename.encode("utf-8")) + len(self.metadata) + len(self.payload)

    def decompress(self) -> bytes:
        codec = get_algorithm(self.algorithm)
        restored = codec.decompress(self.payload, self.metadata)
        if len(restored) != self.original_size:
            raise CorruptDataError("Restored size does not match container metadata")
        if hashlib.sha256(restored).hexdigest() != self.checksum:
            raise CorruptDataError("Checksum mismatch")
        return restored


def pack(data: bytes, filename: str, algorithm: str, progress: ProgressCallback | None = None) -> bytes:
    if not isinstance(data, bytes) or len(data) > MAX_DECOMPRESSED_SIZE:
        raise ContainerError("Input data exceeds the safety limit")
    if not isinstance(filename, str) or not isinstance(algorithm, str):
        raise ContainerError("Filename and algorithm must be strings")
    safe_name = validate_filename(filename)
    name_bytes = safe_name.encode("utf-8")
    if len(name_bytes) > _MAX_NAME_BYTES:
        raise ContainerError("Filename is too long")
    algorithm_name = algorithm.lower()
    try:
        algorithm_id = _ALGORITHM_IDS[algorithm_name]
    except KeyError as error:
        raise ContainerError(f"Unknown algorithm: {algorithm}") from error
    payload, metadata = get_algorithm(algorithm_name).compress(data, progress)
    if len(metadata) > _MAX_METADATA_BYTES or len(payload) > _MAX_PAYLOAD_BYTES:
        raise ContainerError("Algorithm metadata is too large")
    compressed = _make_container(data, safe_name, algorithm_name, payload, metadata)
    stored = _make_container(data, safe_name, "stored", data, b"")
    return compressed if len(compressed) < len(stored) else stored


def _make_container(data: bytes, filename: str, algorithm: str, payload: bytes, metadata: bytes) -> bytes:
    name_bytes = filename.encode("utf-8")
    header = _HEADER.pack(
        MAGIC,
        VERSION,
        _ALGORITHM_IDS[algorithm],
        0,
        len(data),
        len(name_bytes),
        hashlib.sha256(data).digest(),
        len(metadata),
        len(payload),
    )
    return header + name_bytes + metadata + payload


def unpack(raw: bytes) -> Container:
    if len(raw) < _HEADER.size:
        raise ContainerError("Container is truncated")
    try:
        magic, version, algorithm_id, flags, original_size, name_length, checksum, metadata_length, payload_length = _HEADER.unpack_from(raw)
    except struct.error as error:
        raise ContainerError("Invalid container header") from error
    if magic != MAGIC or version != VERSION or flags != 0:
        raise ContainerError("Unsupported container format")
    algorithm = _ID_TO_ALGORITHM.get(algorithm_id)
    if algorithm is None or original_size > MAX_DECOMPRESSED_SIZE or name_length > _MAX_NAME_BYTES or metadata_length > _MAX_METADATA_BYTES or payload_length > _MAX_PAYLOAD_BYTES:
        raise ContainerError("Invalid container metadata")
    end = _HEADER.size + name_length + metadata_length + payload_length
    if end != len(raw):
        raise ContainerError("Container length does not match its header")
    name_start = _HEADER.size
    metadata_start = name_start + name_length
    payload_start = metadata_start + metadata_length
    try:
        filename = raw[name_start:metadata_start].decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContainerError("Filename is not valid UTF-8") from error
    validate_filename(filename)
    return Container(filename, algorithm, original_size, checksum.hex(), raw[metadata_start:payload_start], raw[payload_start:])
