from __future__ import annotations

import argparse
import csv
import time
from pathlib import Path

from filecompression.container import pack, unpack


def run(paths: list[Path], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["file", "algorithm", "original_size", "container_size", "ratio", "saving_percent", "compress_seconds", "decompress_seconds"])
        for path in paths:
            data = path.read_bytes()
            for algorithm in ("huffman", "lzw", "rle"):
                started = time.perf_counter()
                container = pack(data, path.name, algorithm)
                compress_seconds = time.perf_counter() - started
                started = time.perf_counter()
                assert unpack(container).decompress() == data
                decompress_seconds = time.perf_counter() - started
                size = len(container)
                writer.writerow([path.name, algorithm, len(data), size, len(data) / size if size else 0, (1 - size / len(data)) * 100 if data else 0, compress_seconds, decompress_seconds])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, default=Path("benchmark-results/results.csv"))
    arguments = parser.parse_args()
    run(arguments.files, arguments.output)
