"""Who is doing what on the server: uploads in progress and a feed of finished actions.

The API reports here without knowing about the hotspot admin panel. While the panel is
enabled it sets `sink`, which stores each finished action; otherwise records are dropped.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

logger = logging.getLogger("filecompression.web")

UPLOAD = "upload"
UPLOAD_FAILED = "upload_failed"
COMPRESSED = "compressed"
COMPRESS_FAILED = "compress_failed"
COMPRESS_CANCELLED = "compress_cancelled"
DOWNLOAD = "download"
DOWNLOAD_ARCHIVE = "download_archive"
ARCHIVE_IMPORT = "archive_import"
DECOMPRESS = "decompress"


@dataclass
class LiveUpload:
    client: str
    filename: str
    # Original file size when the browser said it; the body is smaller for a gzip upload.
    size: int | None
    # Request body length from Content-Length, the right total for progress.
    body_size: int | None
    received: int = 0
    gzipped: bool = False
    started: float = field(default_factory=time.time)
    identifier: str = field(default_factory=lambda: uuid.uuid4().hex)

    def as_dict(self, now: float) -> dict:
        elapsed = max(now - self.started, 1e-6)
        return {
            "id": self.identifier,
            "kind": UPLOAD,
            "client": self.client,
            "filename": self.filename,
            "size": self.size,
            "total": self.body_size,
            "done": self.received,
            "gzipped": self.gzipped,
            "startedAt": self.started,
            "bytesPerSecond": self.received / elapsed,
        }


live_uploads: dict[str, LiveUpload] = {}
live_lock = threading.Lock()
# Set by the hotspot panel while it is enabled.
sink: Callable[[dict], None] | None = None
# Set by the API: compressions that are queued or running, as dicts like LiveUpload.as_dict.
live_operations: Callable[[], list[dict]] = list


def start_upload(client: str, filename: str, *, size: int | None, body_size: int | None, gzipped: bool) -> LiveUpload:
    upload = LiveUpload(client, filename, size, body_size, gzipped=gzipped)
    with live_lock:
        live_uploads[upload.identifier] = upload
    return upload


def finish_upload(upload: LiveUpload) -> None:
    with live_lock:
        live_uploads.pop(upload.identifier, None)


def live() -> list[dict]:
    now = time.time()
    with live_lock:
        uploads = [upload.as_dict(now) for upload in live_uploads.values()]
    return uploads + live_operations()


def record(client: str, kind: str, **fields) -> None:
    """Store one finished action, e.g. record(ip, UPLOAD, filename="a.txt", size=123)."""
    if sink is None:
        return
    try:
        sink({"at": time.time(), "client": client, "kind": kind, **fields})
    except Exception:  # the activity feed must never break an upload or a download
        logger.exception("cannot record activity %s", kind)
