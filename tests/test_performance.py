import os
import random
import time

import pytest
from fastapi.testclient import TestClient

import filecompression.web.api as web_api
from filecompression.algorithms import _accel, get_algorithm
from filecompression.container.format import _looks_incompressible
from filecompression.errors import CorruptDataError
from filecompression.recommend import CANDIDATES, SIZE_TOLERANCE, recommend
from filecompression.web.api import STORAGE_DIR, app, transfers

native = _accel.native
requires_native = pytest.mark.skipif(native is None, reason="C extension is not built")
_random = random.Random(7)
SAMPLES = [
    b"",
    b"a",
    b"aaaa",
    bytes(range(256)),
    os.urandom(5000),
    b"abababab" * 3000,
    bytes(_random.choices(range(256), weights=[2 ** (index % 20) for index in range(256)], k=200_000)),
    b"".join(bytes([_random.randrange(3)]) * _random.randint(1, 300) for _ in range(2000)),
    # Enough distinct phrases to fill the 16-bit LZW dictionary.
    bytes(_random.choices(b"abcdefgh", k=1_000_000)),
]


def _with(use_native: bool, function, *args):
    saved = _accel.native
    _accel.native = native if use_native else None
    try:
        return function(*args)
    finally:
        _accel.native = saved


@requires_native
@pytest.mark.parametrize("algorithm", ["huffman", "lzw", "rle"])
@pytest.mark.parametrize("data", SAMPLES, ids=range(len(SAMPLES)))
def test_native_codecs_match_pure_python_byte_for_byte(algorithm: str, data: bytes) -> None:
    codec = get_algorithm(algorithm)
    payload, metadata = _with(True, codec.compress, data)
    assert (payload, metadata) == _with(False, codec.compress, data)
    for use_native in (True, False):
        assert _with(use_native, codec.decompress, payload, metadata, max(len(data), 1)) == data


@requires_native
@pytest.mark.parametrize("algorithm", ["huffman", "lzw", "rle"])
def test_native_codecs_reject_corrupt_streams_like_pure_python(algorithm: str) -> None:
    codec = get_algorithm(algorithm)
    generator = random.Random(algorithm)
    for _ in range(300):
        data = generator.choice(SAMPLES[3:8])
        payload, metadata = codec.compress(data)
        damaged = bytearray(payload)
        for _ in range(generator.randint(1, 4)):
            damaged[generator.randrange(len(damaged))] = generator.randrange(256)
        if generator.random() < 0.3:
            damaged = damaged[: generator.randrange(len(damaged))]
        limit = generator.choice([len(data), len(data) // 2])
        outcomes = []
        for use_native in (True, False):
            try:
                outcomes.append(_with(use_native, codec.decompress, bytes(damaged), metadata, limit))
            except CorruptDataError:
                outcomes.append(CorruptDataError)
        assert outcomes[0] == outcomes[1]


def test_recommendation_prefers_the_fastest_codec_close_to_the_smallest() -> None:
    result = recommend(b"the quick brown fox jumps over the lazy dog " * 2000)
    smallest = min(result.estimates.values())
    assert result.algorithm == next(name for name in CANDIDATES if result.estimates[name] <= smallest * SIZE_TOLERANCE)
    assert result.choice == result.algorithm


def test_recommendation_stores_incompressible_data() -> None:
    result = recommend(os.urandom(200_000))
    assert not result.beneficial
    assert result.choice == "stored"
    assert recommend(b"").choice == "stored"


@pytest.mark.parametrize("algorithm", ["huffman", "lzw", "rle"])
def test_incompressible_blocks_skip_the_codec(algorithm: str) -> None:
    assert _looks_incompressible(algorithm, os.urandom(1024 * 1024))
    assert not _looks_incompressible(algorithm, b"plain text block " * 65536)
    assert not _looks_incompressible(algorithm, os.urandom(1000))


def test_high_entropy_block_that_repeats_is_still_compressed() -> None:
    block = os.urandom(4096) * 256
    assert not _looks_incompressible("lzw", block)
    payload, metadata = get_algorithm("lzw").compress(block)
    assert len(payload) + len(metadata) < len(block)


def test_auto_stores_incompressible_upload_without_running_a_codec() -> None:
    transfers.clear()
    data = os.urandom(300_000)
    transfer = TestClient(app).post("/api/transfers", files={"file": ("photo.jpg", data), "algorithm": (None, "auto")}).json()
    assert transfer["algorithm"] == transfer["recommended_algorithm"] == "stored"


def test_download_serves_the_kept_original_and_purge_removes_it() -> None:
    transfers.clear()
    client = TestClient(app)
    data = b"keep me around " * 5000
    identifier = client.post("/api/transfers", files={"file": ("notes.txt", data), "algorithm": (None, "lzw")}).json()["id"]
    original = STORAGE_DIR / f"{identifier}.orig"
    assert original.read_bytes() == data
    download = client.get(f"/api/transfers/{identifier}/download")
    assert download.content == data
    assert download.headers["x-checksum"] == transfers[identifier].info.checksum
    assert 'filename="notes.txt"' in download.headers["content-disposition"]
    assert client.get(f"/api/transfers/{identifier}").json()["status"] == "downloaded"
    transfers[identifier].created_at = time.time() - web_api.TRANSFER_TTL_SECONDS - 1
    web_api._purge_expired()
    assert not original.exists()
    assert not (STORAGE_DIR / f"{identifier}.fcmp").exists()


def test_original_is_dropped_when_disabled_or_the_quota_is_tight(monkeypatch) -> None:
    client = TestClient(app)
    data = b"a" * 100_000
    for setting, quota in ((False, web_api.MAX_TRANSFER_BYTES), (True, 50_000)):
        transfers.clear()
        monkeypatch.setattr(web_api, "KEEP_ORIGINAL", setting)
        monkeypatch.setattr(web_api, "MAX_TRANSFER_BYTES", quota)
        identifier = client.post("/api/transfers", files={"file": ("a.txt", data), "algorithm": (None, "rle")}).json()["id"]
        assert transfers[identifier].original_path is None
        assert not (STORAGE_DIR / f"{identifier}.orig").exists()
        assert client.get(f"/api/transfers/{identifier}/download").content == data


def test_kept_originals_are_reloaded_after_restart() -> None:
    transfers.clear()
    data = b"survives a restart " * 1000
    identifier = TestClient(app).post("/api/transfers", files={"file": ("r.txt", data), "algorithm": (None, "huffman")}).json()["id"]
    orphan = STORAGE_DIR / "orphan.orig"
    orphan.write_bytes(b"no archive")
    transfers.clear()
    web_api._load_persisted_transfers()
    assert transfers[identifier].original_path == STORAGE_DIR / f"{identifier}.orig"
    assert not orphan.exists()
