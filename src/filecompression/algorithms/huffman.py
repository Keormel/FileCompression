from __future__ import annotations

import heapq
import json
from collections import Counter
from dataclasses import dataclass

from filecompression.errors import CorruptDataError
from .base import MAX_DECOMPRESSED_SIZE, CompressionAlgorithm, ProgressCallback


@dataclass
class _Node:
    symbol: int | None
    weight: int = 0
    left: _Node | None = None
    right: _Node | None = None


class HuffmanAlgorithm(CompressionAlgorithm):
    name = "huffman"

    def compress(self, data: bytes, progress: ProgressCallback | None = None) -> tuple[bytes, bytes]:
        if not data:
            return b"", json.dumps({"frequencies": {}, "bit_count": 0}, separators=(",", ":")).encode("ascii")
        counts = Counter(data)
        frequencies = [counts.get(symbol, 0) for symbol in range(256)]

        root = self._build_tree(frequencies)
        codes: dict[int, str] = {}
        self._build_codes(root, "", codes)
        # Concatenating per-symbol bit strings and parsing them as one base-2 integer
        # keeps the per-byte work inside C code (both steps are linear in CPython).
        table = [""] * 256
        for symbol, code in codes.items():
            table[symbol] = code
        bits = "".join(map(table.__getitem__, data))
        total_bits = len(bits)
        padding = -total_bits % 8
        output = int(bits + "0" * padding, 2).to_bytes((total_bits + padding) // 8, "big")
        if progress is not None:
            progress(len(data), len(data))
        metadata = json.dumps(
            {"frequencies": {str(index): count for index, count in enumerate(frequencies) if count}, "bit_count": total_bits},
            separators=(",", ":"),
        ).encode("ascii")
        return output, metadata

    def decompress(self, payload: bytes, metadata: bytes, limit: int = MAX_DECOMPRESSED_SIZE) -> bytes:
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
        if expected_size > min(limit, MAX_DECOMPRESSED_SIZE):
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

        # Byte-at-a-time state machine: a state is an internal tree node, and each
        # (state, input byte) pair maps to the symbols it emits plus the next state.
        # Entries are built lazily, so only the pairs present in the stream cost anything.
        internal: list[_Node] = []
        state_of: dict[int, int] = {}
        pending = [root]
        while pending:
            node = pending.pop()
            if node.symbol is None:
                state_of[id(node)] = len(internal)
                internal.append(node)
                pending.extend((node.left, node.right))
        transitions: dict[int, tuple[bytes, int]] = {}

        def walk(state: int, value: int, bits: int) -> tuple[bytes, int]:
            node = internal[state]
            emitted = bytearray()
            for shift in range(7, 7 - bits, -1):
                node = node.right if (value >> shift) & 1 else node.left
                if node.symbol is not None:
                    emitted.append(node.symbol)
                    node = root
            return bytes(emitted), state_of[id(node)]

        output = bytearray()
        state = 0
        full_bytes = bit_count // 8
        for value in payload[:full_bytes]:
            key = state << 8 | value
            entry = transitions.get(key)
            if entry is None:
                entry = transitions[key] = walk(state, value, 8)
            emitted, state = entry
            output += emitted
            if len(output) > expected_size:
                raise CorruptDataError("Huffman stream contains extra data")
        if bit_count % 8:
            last = payload[-1]
            if last & ((1 << (8 - bit_count % 8)) - 1):
                raise CorruptDataError("Invalid Huffman padding")
            emitted, state = walk(state, last, bit_count % 8)
            output += emitted
        if len(output) > expected_size:
            raise CorruptDataError("Huffman stream contains extra data")
        if len(output) == expected_size and state == 0:
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
