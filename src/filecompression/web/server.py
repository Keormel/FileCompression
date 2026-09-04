from __future__ import annotations

import argparse
import logging
import os

import uvicorn

from .api import app


def main() -> None:
    logging.basicConfig(
        level=os.getenv("FILECOMP_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(description="Run FileComp Web UI API")
    parser.add_argument("--host", default=os.getenv("FILECOMP_WEB_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("FILECOMP_WEB_PORT", "8000")))
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
