import json

import pytest

from filecompression.algorithms import get_algorithm
from filecompression.errors import CorruptDataError


@pytest.mark.parametrize("algorithm", ["huffman", "lzw", "rle"])
@pytest.mark.parametrize("data", [b"", b"x", b"x" * 4096, bytes(range(256)), bytes(range(256)) * 32, b"text " * 1000, bytes((index * 73) % 256 for index in range(10000))])
def test_round_trip(algorithm: str, data: bytes) -> None:
    codec = get_algorithm(algorithm)
    payload, metadata = codec.compress(data)
    assert codec.decompress(payload, metadata) == data


def test_corrupt_huffman_metadata_is_rejected() -> None:
    codec = get_algorithm("huffman")
    with pytest.raises(CorruptDataError):
        codec.decompress(b"", b"not-json")


def test_huffman_rejects_trailing_bits() -> None:
    codec = get_algorithm("huffman")
    payload, metadata = codec.compress(b"ababa")
    decoded = json.loads(metadata)
    decoded["bit_count"] -= 1
    with pytest.raises(CorruptDataError):
        codec.decompress(payload, json.dumps(decoded).encode("ascii"))


def test_rle_rejects_zero_run() -> None:
    codec = get_algorithm("rle")
    with pytest.raises(CorruptDataError):
        codec.decompress(b"\x00\x00\x00\x00A", b"")


def test_lzw_rejects_invalid_first_code() -> None:
    codec = get_algorithm("lzw")
    with pytest.raises(CorruptDataError):
        codec.decompress(b"\x01\x00", b'{"code_width":16}')
