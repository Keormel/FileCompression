import pytest

from filecompression.container import pack, unpack
from filecompression.errors import ContainerError, CorruptDataError


@pytest.mark.parametrize("algorithm", ["huffman", "lzw", "rle"])
def test_container_round_trip(algorithm: str) -> None:
    data = bytes(range(256)) * 3
    container = unpack(pack(data, "sample.bin", algorithm))
    assert container.filename == "sample.bin"
    assert container.algorithm == algorithm
    assert container.decompress() == data


def test_container_rejects_path_traversal() -> None:
    with pytest.raises(ContainerError):
        pack(b"data", "../unsafe.bin", "rle")


def test_container_detects_checksum_corruption() -> None:
    raw = bytearray(pack(b"data", "sample.bin", "rle"))
    raw[-1] ^= 1
    with pytest.raises(CorruptDataError):
        unpack(bytes(raw)).decompress()
