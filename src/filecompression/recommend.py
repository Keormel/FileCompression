from __future__ import annotations

from dataclasses import dataclass

from filecompression.algorithms import get_algorithm

CANDIDATES = ("lzw", "huffman", "rle")
SAMPLE_LIMIT = 1024 * 1024
SAMPLE_CHUNKS = 4


@dataclass(frozen=True)
class Recommendation:
    algorithm: str
    sample_size: int
    estimates: dict[str, int]

    @property
    def beneficial(self) -> bool:
        return self.estimates[self.algorithm] < self.sample_size

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


def recommend(data: bytes) -> Recommendation:
    """Compress a sample with every codec and pick the one with the smallest output."""
    probe = sample(data)
    estimates = {}
    for name in CANDIDATES:
        payload, metadata = get_algorithm(name).compress(probe)
        estimates[name] = len(payload) + len(metadata)
    best = min(CANDIDATES, key=lambda name: estimates[name])
    return Recommendation(best, len(probe), estimates)
