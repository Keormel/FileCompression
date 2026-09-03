from __future__ import annotations

import heapq
import json
from dataclasses import dataclass

from filecompression.errors import CorruptDataError
from .base import MAX_DECOMPRESSED_SIZE, CompressionAlgorithm


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
        if not data:
            return b"", json.dumps({"frequencies": {}, "bit_count": 0}, separators=(",", ":")).encode("ascii")

        root = self._build_tree(frequencies)
        codes: dict[int, str] = {}
        self._build_codes(root, "", codes)
        output = bytearray()
        current = 0
        pending_bits = 0
        total_bits = 0
        for value in data:
            for bit in codes[value]:
                current = (current << 1) | int(bit)
                pending_bits += 1
                total_bits += 1
                if pending_bits == 8:
                    output.append(current)
                    current = pending_bits = 0
        if pending_bits:
            output.append(current << (8 - pending_bits))
        metadata = json.dumps(
            {"frequencies": {str(index): count for index, count in enumerate(frequencies) if count}, "bit_count": total_bits},
            separators=(",", ":"),
        ).encode("ascii")
        return bytes(output), metadata

    def decompress(self, payload: bytes, metadata: bytes) -> bytes:
        try:
            decoded = json.loads(metadata.decode("ascii"))
            if set(decoded) != {"frequencies", "bit_count"} or not isinstance(decoded["frequencies"], dict):
                raise ValueError
            frequencies = decoded["frequencies"]
            table = [0] * 256
            for key, count in frequencies.items():
                symbol = int(key)
                if not 0 <= symbol < 256 or not isinstance(count, int) or count <= 0:
                    raise ValueError
                table[symbol] = count
        except (ValueError, TypeError, KeyError, json.JSONDecodeError, UnicodeDecodeError) as error:
            raise CorruptDataError("Invalid Huffman metadata") from error

        expected_size = sum(table)
        if expected_size > MAX_DECOMPRESSED_SIZE:
            raise CorruptDataError("Huffman output exceeds the safety limit")
        try:
            bit_count = decoded["bit_count"]
        except (KeyError, TypeError):
            raise CorruptDataError("Missing Huffman bit count") from None
        if not isinstance(bit_count, int) or bit_count < 0 or bit_count > len(payload) * 8 or len(payload) != (bit_count + 7) // 8:
            raise CorruptDataError("Invalid Huffman bit count")
        if expected_size == 0:
            if payload or bit_count:
                raise CorruptDataError("Non-empty payload for an empty Huffman stream")
            return b""
        root = self._build_tree(table)
        if root.symbol is not None:
            if bit_count != expected_size or len(payload) != (expected_size + 7) // 8:
                raise CorruptDataError("Invalid single-symbol Huffman payload")
            if any(payload[:-1]) or (payload and payload[-1] & ((1 << (8 - bit_count % 8)) - 1 if bit_count % 8 else 0)):
                raise CorruptDataError("Invalid single-symbol Huffman padding")
            return bytes([root.symbol]) * expected_size

        codes: dict[int, str] = {}
        self._build_codes(root, "", codes)
        reverse_codes = {code: symbol for symbol, code in codes.items()}
        output = bytearray()
        current_code = ""
        for index in range(bit_count):
            byte = payload[index // 8]
            shift = 7 - index % 8
            current_code += "1" if (byte >> shift) & 1 else "0"
            symbol = reverse_codes.get(current_code)
            if symbol is not None:
                output.append(symbol)
                if len(output) > expected_size:
                    raise CorruptDataError("Huffman stream contains extra data")
                current_code = ""
        if len(output) == expected_size and not current_code:
            if bit_count % 8 and payload[-1] & ((1 << (8 - bit_count % 8)) - 1):
                raise CorruptDataError("Invalid Huffman padding")
            return bytes(output)
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
