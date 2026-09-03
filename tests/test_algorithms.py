import pytest

from filecompression.algorithms import get_algorithm
from filecompression.errors import CorruptDataError


@pytest.mark.parametrize("algorithm", ["huffman", "lzw", "rle"])
@pytest.mark.parametrize("data", [b"", b"x", b"x" * 4096, bytes(range(256)), b"text " * 1000])
def test_round_trip(algorithm: str, data: bytes) -> None:
    codec = get_algorithm(algorithm)
    payload, metadata = codec.compress(data)
    assert codec.decompress(payload, metadata) == data


def test_corrupt_huffman_metadata_is_rejected() -> None:
    codec = get_algorithm("huffman")
    with pytest.raises(CorruptDataError):
        codec.decompress(b"", b"not-json")
