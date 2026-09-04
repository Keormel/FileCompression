from __future__ import annotations

import json
import struct

from filecompression.errors import CorruptDataError
from .base import MAX_DECOMPRESSED_SIZE, CompressionAlgorithm, ProgressCallback


class LzwAlgorithm(CompressionAlgorithm):
    name = "lzw"
    _maximum_code = 65535

    def compress(self, data: bytes, progress: ProgressCallback | None = None) -> tuple[bytes, bytes]:
        if not data:
            return b"", json.dumps({"code_width": 16}).encode("ascii")
        dictionary = {bytes([value]): value for value in range(256)}
        next_code = 256
        phrase = bytes([data[0]])
        codes: list[int] = []
        for index, value in enumerate(data[1:], 2):
            candidate = phrase + bytes([value])
            if candidate in dictionary:
                phrase = candidate
                continue
            codes.append(dictionary[phrase])
            if next_code <= self._maximum_code:
                dictionary[candidate] = next_code
                next_code += 1
            phrase = bytes([value])
            if progress is not None and (index == len(data) or index % (1024 * 1024) == 0):
                progress(index, len(data))
        codes.append(dictionary[phrase])
        return b"".join(struct.pack(">H", code) for code in codes), json.dumps({"code_width": 16}).encode("ascii")

    def decompress(self, payload: bytes, metadata: bytes) -> bytes:
        try:
            decoded_metadata = json.loads(metadata.decode("ascii"))
            if set(decoded_metadata) != {"code_width"} or decoded_metadata["code_width"] != 16 or len(payload) % 2:
                raise ValueError
        except (ValueError, KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as error:
            raise CorruptDataError("Invalid LZW metadata or payload length") from error
        if not payload:
            return b""
        codes = [struct.unpack(">H", payload[index:index + 2])[0] for index in range(0, len(payload), 2)]
        if codes[0] > 255:
            raise CorruptDataError("Invalid first LZW code")
        dictionary = {value: bytes([value]) for value in range(256)}
        next_code = 256
        previous = dictionary[codes[0]]
        output = bytearray(previous)
        for code in codes[1:]:
            if code in dictionary:
                entry = dictionary[code]
            elif code == next_code:
                entry = previous + previous[:1]
            else:
                raise CorruptDataError("Invalid LZW code")
            if len(output) + len(entry) > MAX_DECOMPRESSED_SIZE:
                raise CorruptDataError("LZW output exceeds the safety limit")
            output.extend(entry)
            if next_code <= self._maximum_code:
                dictionary[next_code] = previous + entry[:1]
                next_code += 1
            previous = entry
        return bytes(output)
