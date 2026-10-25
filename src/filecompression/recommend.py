from __future__ import annotations

import os
from dataclasses import dataclass

from filecompression.algorithms import get_algorithm

# Fastest codec first: when outputs are within SIZE_TOLERANCE of the smallest, the
# faster codec wins, because LZW is noticeably slower than RLE and Huffman.
CANDIDATES = ("rle", "huffman", "lzw")
SIZE_TOLERANCE = 1.10
# Below this saving AUTO stores the file as is instead of running a codec over it.
MIN_SAVING = 0.02
SAMPLE_LIMIT = 1024 * 1024
SAMPLE_CHUNKS = 4


@dataclass(frozen=True)
class Recommendation:
    algorithm: str
    sample_size: int
    estimates: dict[str, int]

    @property
    def beneficial(self) -> bool:
        return self.sample_size > 0 and self.estimates[self.algorithm] <= self.sample_size * (1 - MIN_SAVING)

    @property
    def choice(self) -> str:
        """Algorithm AUTO mode uses: ``stored`` when no codec shrinks the file noticeably."""
        return self.algorithm if self.beneficial else "stored"

    def as_dict(self) -> dict:
        return {
            "recommended": self.algorithm,
            "beneficial": self.beneficial,
            "sampleSize": self.sample_size,
            "estimates": [
                {"algorithm": name, "size": size, "ratio": self.sample_size / size if size else 0.0}
                for name, size in sorted(self.estimates.items(), key=lambda item: item[1])
            ],
        }


def sample(data: bytes) -> bytes:
    """Take evenly spaced chunks so large files are estimated from start, middle and end."""
    if len(data) <= SAMPLE_LIMIT:
        return data
    chunk = SAMPLE_LIMIT // SAMPLE_CHUNKS
    step = (len(data) - chunk) // (SAMPLE_CHUNKS - 1)
    return b"".join(data[index * step : index * step + chunk] for index in range(SAMPLE_CHUNKS))


def sample_file(path: str | os.PathLike) -> bytes:
    """Same sampling as :func:`sample`, but reads only the sampled ranges from disk."""
    size = os.path.getsize(path)
    with open(path, "rb") as handle:
        if size <= SAMPLE_LIMIT:
            return handle.read()
        chunk = SAMPLE_LIMIT // SAMPLE_CHUNKS
        step = (size - chunk) // (SAMPLE_CHUNKS - 1)
        parts = []
        for index in range(SAMPLE_CHUNKS):
            handle.seek(index * step)
            parts.append(handle.read(chunk))
        return b"".join(parts)


def recommend_file(path: str | os.PathLike) -> Recommendation:
    return recommend(sample_file(path))


def recommend(data: bytes) -> Recommendation:
    """Compress a sample with every codec; pick the fastest one close to the smallest output."""
    probe = sample(data)
    estimates = {}
    for name in CANDIDATES:
        payload, metadata = get_algorithm(name).compress(probe)
        estimates[name] = len(payload) + len(metadata)
    smallest = min(estimates.values())
    best = next(name for name in CANDIDATES if estimates[name] <= smallest * SIZE_TOLERANCE)
    return Recommendation(best, len(probe), estimates)
