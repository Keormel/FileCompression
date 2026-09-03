from __future__ import annotations

import argparse

import uvicorn

from .api import app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run FileComp Web UI API")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
