from __future__ import annotations

import argparse
import logging
import os
import tempfile
from pathlib import Path


def _prepare_storage() -> None:
    """Spool multipart uploads to the storage volume instead of a small /tmp (tmpfs in Docker)."""
    storage = Path(os.getenv("FILECOMP_STORAGE_DIR", str(Path.cwd() / "data")))
    spool = storage / "tmp"
    spool.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(spool)


def main() -> None:
    logging.basicConfig(
        level=os.getenv("FILECOMP_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(description="Run FileComp Web UI API")
    parser.add_argument("--host", default=os.getenv("FILECOMP_WEB_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("FILECOMP_WEB_PORT", "8000")))
    args = parser.parse_args()
    _prepare_storage()

    # Imported here, not at module level: compression worker processes re-import this
    # module and must not build a second application.
    import uvicorn

    from .api import app

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        # One process: transfers and operations live in this process's memory.
        workers=1,
        timeout_keep_alive=30,
        # Beyond this many open connections new requests get an immediate 503 instead of piling up
        # until the process runs out of memory or file descriptors.
        limit_concurrency=int(os.getenv("FILECOMP_MAX_CONNECTIONS", "256") or "256"),
        proxy_headers=True,
        forwarded_allow_ips=os.getenv("FILECOMP_FORWARDED_ALLOW_IPS", "127.0.0.1"),
        server_header=False,
    )


if __name__ == "__main__":
    main()
