from __future__ import annotations

import heapq
import json
from dataclasses import dataclass

from filecompression.errors import CorruptDataError
from .base import CompressionAlgorithm


@dataclass
class _Node:
    symbol: int | None
    weight: int = 0
    left: _Node | None = None
    right: _Node | None = None


class HuffmanAlgorithm(CompressionAlgorithm):
    name = "huffman"

    def compress(self, data: bytes) -> tuple[bytes, bytes]:
        frequencies = [0] * 256
        for value in data:
            frequencies[value] += 1
        metadata = json.dumps(
            {"frequencies": {str(index): count for index, count in enumerate(frequencies) if count}},
            separators=(",", ":"),
        ).encode("ascii")
        if not data:
            return b"", metadata

        root = self._build_tree(frequencies)
        codes: dict[int, str] = {}
        self._build_codes(root, "", codes)
        output = bytearray()
        current = 0
        bit_count = 0
        for value in data:
            for bit in codes[value]:
                current = (current << 1) | int(bit)
                bit_count += 1
                if bit_count == 8:
                    output.append(current)
                    current = bit_count = 0
        if bit_count:
            output.append(current << (8 - bit_count))
        return bytes(output), metadata

    def decompress(self, payload: bytes, metadata: bytes) -> bytes:
        try:
            frequencies = json.loads(metadata.decode("ascii"))["frequencies"]
            table = [0] * 256
            for key, count in frequencies.items():
                symbol = int(key)
                if not 0 <= symbol < 256 or not isinstance(count, int) or count <= 0:
                    raise ValueError
                table[symbol] = count
        except (ValueError, TypeError, KeyError, json.JSONDecodeError, UnicodeDecodeError) as error:
            raise CorruptDataError("Invalid Huffman metadata") from error

        expected_size = sum(table)
        if expected_size == 0:
            if payload:
                raise CorruptDataError("Non-empty payload for an empty Huffman stream")
            return b""
        root = self._build_tree(table)
        if root.symbol is not None:
            if payload and any(payload):
                raise CorruptDataError("Invalid single-symbol Huffman payload")
            return bytes([root.symbol]) * expected_size

        output = bytearray()
        node = root
        for byte in payload:
            for shift in range(7, -1, -1):
                node = node.right if (byte >> shift) & 1 else node.left
                if node is None:
                    raise CorruptDataError("Invalid Huffman bit stream")
                if node.symbol is not None:
                    output.append(node.symbol)
                    if len(output) == expected_size:
                        return bytes(output)
                    node = root
        raise CorruptDataError("Truncated Huffman bit stream")

    @staticmethod
    def _build_tree(frequencies: list[int]) -> _Node:
        heap: list[tuple[int, int, _Node]] = []
        order = 0
        for symbol, frequency in enumerate(frequencies):
            if frequency:
                heapq.heappush(heap, (frequency, order, _Node(symbol, frequency)))
                order += 1
        if not heap:
            return _Node(None)
        while len(heap) > 1:
            left = heapq.heappop(heap)[2]
            right = heapq.heappop(heap)[2]
            weight = left.weight + right.weight
            heapq.heappush(heap, (weight, order, _Node(None, weight, left, right)))
            order += 1
        return heap[0][2]

    @staticmethod
    def _build_codes(node: _Node, prefix: str, codes: dict[int, str]) -> None:
        if node.symbol is not None:
            codes[node.symbol] = prefix or "0"
            return
        if node.left is not None:
            HuffmanAlgorithm._build_codes(node.left, prefix + "0", codes)
        if node.right is not None:
            HuffmanAlgorithm._build_codes(node.right, prefix + "1", codes)
