import hashlib
import io
import os
import struct
from concurrent.futures import ThreadPoolExecutor

import pytest

from filecompression.container import iter_decompress, pack_stream, read_info, unpack, unpack_stream
from filecompression.errors import ContainerError, CorruptDataError, OperationCancelled
from filecompression.recommend import recommend, sample, sample_file
from filecompression.service import compress_file, decompress_file


def _mixed_data(size: int) -> bytes:
    text = b"The quick brown fox jumps over the lazy dog. " * (size // 45 + 1)
    noise = os.urandom(size // 4)
    return (text[: size // 2] + noise + b"\x00" * (size // 4))[:size]


def _pack(data: bytes, algorithm: str, block_size: int, executor=None) -> tuple[bytes, object]:
    output = io.BytesIO()
    info = pack_stream(io.BytesIO(data), output, "big.bin", algorithm, block_size=block_size, executor=executor)
    return output.getvalue(), info


@pytest.mark.parametrize("algorithm", ["huffman", "lzw", "rle", "stored"])
def test_multi_block_round_trip(algorithm: str) -> None:
    data = _mixed_data(300_000)
    raw, info = _pack(data, algorithm, 64 * 1024)
    assert info.block_count == 5
    assert info.original_size == len(data)
    assert info.checksum == hashlib.sha256(data).hexdigest()
    assert info.compressed_size == len(raw)
    assert read_info(io.BytesIO(raw)) == info
    assert b"".join(iter_decompress(io.BytesIO(raw))) == data
    assert unpack(raw).decompress() == data


def test_incompressible_blocks_fall_back_to_stored() -> None:
    data = os.urandom(100_000)
    raw, info = _pack(data, "huffman", 32 * 1024)
    assert info.algorithm == "stored"
    assert len(raw) < len(data) + 200


def test_parallel_executor_produces_identical_container() -> None:
    data = _mixed_data(200_000)
    sequential, _ = _pack(data, "lzw", 16 * 1024)
    with ThreadPoolExecutor(4) as executor:
        parallel, _ = _pack(data, "lzw", 16 * 1024, executor)
        assert b"".join(iter_decompress(io.BytesIO(parallel), executor=executor)) == data
    assert parallel == sequential


def test_corrupted_block_payload_is_detected() -> None:
    data = b"abc" * 50_000
    raw = bytearray(_pack(data, "stored", 32 * 1024)[0])
    raw[len(raw) // 2] ^= 0xFF
    with pytest.raises(CorruptDataError):
        b"".join(iter_decompress(io.BytesIO(bytes(raw))))


def test_truncated_stream_is_rejected() -> None:
    raw = _pack(b"hello world" * 10_000, "huffman", 16 * 1024)[0]
    for cut in (len(raw) - 1, len(raw) - 40, len(raw) // 2):
        with pytest.raises(ContainerError):
            read_info(io.BytesIO(raw[:cut]))


def test_empty_file_round_trip() -> None:
    raw, info = _pack(b"", "huffman", 1024)
    assert info.algorithm == "stored"
    assert info.block_count == 0
    assert unpack(raw).decompress() == b""


def test_cancellation_stops_compression() -> None:
    calls = iter(range(100))
    with pytest.raises(OperationCancelled):
        pack_stream(io.BytesIO(b"x" * 100_000), io.BytesIO(), "a.bin", "rle", block_size=1024, cancelled=lambda: next(calls) > 3)


def test_version_1_containers_remain_readable() -> None:
    data = b"legacy " * 100
    name = b"old.txt"
    header = struct.pack(">4sBBHQH32sIQ", b"FCMP", 1, 4, 0, len(data), len(name), hashlib.sha256(data).digest(), 0, len(data))
    raw = header + name + data
    info = read_info(io.BytesIO(raw))
    assert (info.version, info.filename, info.algorithm) == (1, "old.txt", "stored")
    assert unpack(raw).decompress() == data
    output = io.BytesIO()
    unpack_stream(io.BytesIO(raw), output)
    assert output.getvalue() == data


def test_file_service_streams_round_trip(tmp_path) -> None:
    data = _mixed_data(150_000)
    source = tmp_path / "input.bin"
    source.write_bytes(data)
    stats = compress_file(source, tmp_path / "out.fcmp", "huffman")
    assert stats.original_size == len(data)
    decompress_file(tmp_path / "out.fcmp", tmp_path / "restored.bin")
    assert (tmp_path / "restored.bin").read_bytes() == data


def test_file_sampling_matches_in_memory_sampling(tmp_path) -> None:
    data = _mixed_data(5_000_000)
    path = tmp_path / "sample.bin"
    path.write_bytes(data)
    assert sample_file(path) == sample(data)
    assert recommend(sample_file(path)).algorithm in {"huffman", "lzw", "rle"}
