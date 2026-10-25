"""Optional C hot loops (``filecompression._speedups``).

The extension is built from ``src/filecompression/_speedups.c`` when a C compiler is
available and produces exactly the same bytes as the pure Python codecs. Without it,
or with ``FILECOMP_PURE_PYTHON=1``, the Python implementation is used.
"""

from __future__ import annotations

import os
from collections import Counter

native = None
if os.getenv("FILECOMP_PURE_PYTHON", "").strip().lower() not in ("1", "true", "yes"):
    try:
        from filecompression import _speedups as native
    except ImportError:
        native = None


def histogram(data: bytes) -> list[int]:
    """Count of every byte value 0-255."""
    if native is not None:
        return native.histogram(data)
    counts = Counter(data)
    return [counts.get(symbol, 0) for symbol in range(256)]
