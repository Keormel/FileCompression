from __future__ import annotations

import argparse
import csv
import json
import platform
import time
import tracemalloc
from pathlib import Path

from filecompression.container import pack, unpack


def generate_fixtures(directory: Path) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    repetitive = ("A" * 500 + "B" * 250 + "\n") * 200
    ordinary = "File compression research sample.\n" * 2000
    (directory / "text_small.txt").write_text(ordinary[:2000], encoding="utf-8")
    (directory / "text_large.txt").write_text(repetitive + ordinary, encoding="utf-8")
    (directory / "data.json").write_text(json.dumps({"items": [{"id": index, "value": "sample"} for index in range(1000)]}), encoding="utf-8")
    (directory / "data.csv").write_text("id,value\n" + "\n".join(f"{index},sample" for index in range(1000)), encoding="utf-8")
    (directory / "random.bin").write_bytes(bytes((index * 73 + 19) % 256 for index in range(100_000)))
    return sorted(directory.glob("*"))


def run(paths: list[Path], output: Path) -> list[dict]:
    output.parent.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    with output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["file", "file_type", "algorithm", "original_size", "compressed_size", "payload_size", "compression_ratio", "space_saving_percent", "compression_time", "decompression_time", "compression_throughput", "decompression_throughput", "peak_memory"])
        for path in paths:
            data = path.read_bytes()
            for algorithm in ("huffman", "lzw", "rle"):
                tracemalloc.start()
                started = time.perf_counter()
                container = pack(data, path.name, algorithm)
                compress_seconds = time.perf_counter() - started
                started = time.perf_counter()
                assert unpack(container).decompress() == data
                decompress_seconds = time.perf_counter() - started
                _, peak_memory = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                size = len(container)
                result = {"file": path.name, "file_type": path.suffix.lstrip(".") or "bin", "algorithm": algorithm, "original_size": len(data), "compressed_size": size, "payload_size": len(unpack(container).payload), "compression_ratio": len(data) / size if size else 0, "space_saving_percent": (1 - size / len(data)) * 100 if data else 0, "compression_time": compress_seconds, "decompression_time": decompress_seconds, "compression_throughput": len(data) / compress_seconds if compress_seconds else 0, "decompression_throughput": len(data) / decompress_seconds if decompress_seconds else 0, "peak_memory": peak_memory}
                results.append(result)
                writer.writerow(result.values())
    output.with_suffix(".json").write_text(json.dumps({"python": platform.python_version(), "platform": platform.platform(), "results": results}, indent=2), encoding="utf-8")
    output.with_suffix(".md").write_text("# Benchmark results\n\n" + "\n".join("| " + " | ".join(str(value) for value in result.values()) + " |" for result in results), encoding="utf-8")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="*", type=Path)
    parser.add_argument("--generate-fixtures", type=Path)
    parser.add_argument("--output", type=Path, default=Path("benchmark-results/results.csv"))
    arguments = parser.parse_args()
    paths = generate_fixtures(arguments.generate_fixtures) if arguments.generate_fixtures else arguments.files
    if not paths:
        parser.error("provide files or --generate-fixtures")
    run(paths, arguments.output)
