import pytest
import struct

from filecompression.container import pack, unpack
from filecompression.algorithms.base import MAX_DECOMPRESSED_SIZE
from filecompression.algorithms import get_algorithm
from filecompression.errors import ContainerError, CorruptDataError


@pytest.mark.parametrize("algorithm", ["huffman", "lzw", "rle"])
def test_container_round_trip(algorithm: str) -> None:
    data = bytes(range(256)) * 3
    container = unpack(pack(data, "sample.bin", algorithm))
    assert container.filename == "sample.bin"
    assert container.algorithm in {algorithm, "stored"}
    assert container.decompress() == data


def test_container_rejects_path_traversal() -> None:
    with pytest.raises(ContainerError):
        pack(b"data", "../unsafe.bin", "rle")


def test_container_detects_checksum_corruption() -> None:
    raw = bytearray(pack(b"data", "sample.bin", "rle"))
    raw[-1] ^= 1
    with pytest.raises(CorruptDataError):
        unpack(bytes(raw)).decompress()


@pytest.mark.parametrize("offset,value", [(0, ord("X")), (4, 2), (5, 99)])
def test_container_rejects_invalid_header(offset: int, value: int) -> None:
    raw = bytearray(pack(b"data", "sample.bin", "rle"))
    raw[offset] = value
    with pytest.raises(ContainerError):
        unpack(bytes(raw))


def test_container_rejects_truncation_and_trailing_bytes() -> None:
    raw = pack(b"data", "sample.bin", "rle")
    with pytest.raises(ContainerError):
        unpack(raw[:-1])
    with pytest.raises(ContainerError):
        unpack(raw + b"junk")


def test_container_rejects_unsafe_decoded_filename() -> None:
    raw = bytearray(pack(b"data", "sample.bin", "rle"))
    name_start = struct.calcsize(">4sBBHQH32sIQ")
    raw[name_start:name_start + len("sample.bin")] = b"..\\evilbin"
    with pytest.raises(ContainerError):
        unpack(bytes(raw))


def test_rle_output_limit_is_checked_before_allocation() -> None:
    payload = (MAX_DECOMPRESSED_SIZE + 1).to_bytes(4, "big") + b"A"
    with pytest.raises(CorruptDataError):
        get_algorithm("rle").decompress(payload, b"")
