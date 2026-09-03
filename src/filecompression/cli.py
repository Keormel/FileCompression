from __future__ import annotations

import argparse
from pathlib import Path

from .service import compress_file, decompress_file


def main() -> None:
    parser = argparse.ArgumentParser(description="Lossless file compression utility")
    commands = parser.add_subparsers(dest="command", required=True)
    compress = commands.add_parser("compress")
    compress.add_argument("source", type=Path)
    compress.add_argument("destination", type=Path)
    compress.add_argument("--algorithm", choices=("huffman", "lzw", "rle"), required=True)
    decompress = commands.add_parser("decompress")
    decompress.add_argument("source", type=Path)
    decompress.add_argument("destination", type=Path)
    args = parser.parse_args()
    if args.command == "compress":
        stats = compress_file(args.source, args.destination, args.algorithm)
        print(f"Original size: {stats.original_size}")
        print(f"Compressed size: {stats.compressed_size}")
        print(f"Compression ratio: {stats.ratio:.4f}")
        print(f"Space saving: {stats.saving_percent:.2f}%")
        print(f"Compression time: {stats.compression_time:.6f}s")
        print(f"Decompression time: {stats.decompression_time:.6f}s")
    else:
        decompress_file(args.source, args.destination)
        print(f"Restored file: {args.destination}")


if __name__ == "__main__":
    main()
