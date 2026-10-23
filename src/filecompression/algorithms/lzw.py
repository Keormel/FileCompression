from __future__ import annotations

import json
import sys
from array import array

from filecompression.errors import CorruptDataError
from .base import MAX_DECOMPRESSED_SIZE, CompressionAlgorithm, ProgressCallback


def _codes_to_bytes(codes: array) -> bytes:
    if sys.byteorder == "little":
        codes.byteswap()
    return codes.tobytes()


class LzwAlgorithm(CompressionAlgorithm):
    name = "lzw"
    _maximum_code = 65535

    def compress(self, data: bytes, progress: ProgressCallback | None = None) -> tuple[bytes, bytes]:
        if not data:
            return b"", json.dumps({"code_width": 16}).encode("ascii")
        # A phrase is identified by its code, so "phrase + byte" is the integer key
        # (code << 8 | byte); codes 0-255 are the single-byte phrases.
        dictionary: dict[int, int] = {}
        next_code = 256
        phrase = data[0]
        codes = array("H")
        for value in memoryview(data)[1:]:
            key = phrase << 8 | value
            code = dictionary.get(key)
            if code is not None:
                phrase = code
                continue
            codes.append(phrase)
            if next_code <= self._maximum_code:
                dictionary[key] = next_code
                next_code += 1
            phrase = value
        codes.append(phrase)
        if progress is not None:
            progress(len(data), len(data))
        return _codes_to_bytes(codes), json.dumps({"code_width": 16}).encode("ascii")

    def decompress(self, payload: bytes, metadata: bytes, limit: int = MAX_DECOMPRESSED_SIZE) -> bytes:
        try:
            decoded_metadata = json.loads(metadata.decode("ascii"))
            if set(decoded_metadata) != {"code_width"} or decoded_metadata["code_width"] != 16 or len(payload) % 2:
                raise ValueError
        except (ValueError, KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as error:
            raise CorruptDataError("Invalid LZW metadata or payload length") from error
        if not payload:
            return b""
        limit = min(limit, MAX_DECOMPRESSED_SIZE)
        codes = array("H")
        codes.frombytes(payload)
        if sys.byteorder == "little":
            codes.byteswap()
        if codes[0] > 255:
            raise CorruptDataError("Invalid first LZW code")
        dictionary = [bytes((value,)) for value in range(256)]
        previous = dictionary[codes[0]]
        output = bytearray(previous)
        for code in codes[1:]:
            if code < len(dictionary):
                entry = dictionary[code]
            elif code == len(dictionary):
                entry = previous + previous[:1]
            else:
                raise CorruptDataError("Invalid LZW code")
            output += entry
            if len(output) > limit:
                raise CorruptDataError("LZW output exceeds the safety limit")
            if len(dictionary) <= self._maximum_code:
                dictionary.append(previous + entry[:1])
            previous = entry
        return bytes(output)
