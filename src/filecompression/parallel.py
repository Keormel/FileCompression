"""Shared process pool for block compression.

The codecs are pure Python, so threads cannot use more than one CPU core. Blocks of
the FCMP v2 container are independent, which lets a process pool spread them over
all cores (four on a Raspberry Pi 4/5).
"""

from __future__ import annotations

import multiprocessing
import os
import sys
import threading
from concurrent.futures import Executor, ProcessPoolExecutor

_lock = threading.Lock()
_executor: ProcessPoolExecutor | None = None


def configured_workers() -> int:
    value = os.getenv("FILECOMP_WORKERS", "").strip()
    if value:
        return max(1, int(value))
    return max(1, os.cpu_count() or 1)


def shared_executor() -> Executor | None:
    """Return the process pool, or ``None`` when only one worker is configured."""
    global _executor
    workers = configured_workers()
    if workers <= 1:
        return None
    with _lock:
        if _executor is None:
            # fork() from a process that already runs server threads is unsafe.
            method = "forkserver" if sys.platform.startswith("linux") else "spawn"
            _executor = ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context(method))
        return _executor


def pipeline_depth(executor: Executor | None) -> int:
    """Blocks kept in flight: enough to keep every worker busy, small enough to bound memory."""
    return 1 if executor is None else configured_workers() * 2


def shutdown() -> None:
    global _executor
    with _lock:
        if _executor is not None:
            _executor.shutdown(wait=False, cancel_futures=True)
            _executor = None
