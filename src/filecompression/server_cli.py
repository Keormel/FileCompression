from __future__ import annotations

import argparse
import asyncio

from .transfer import TransferServer


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the FileCompression relay server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = TransferServer(args.host, args.port)
    try:
        asyncio.run(server.serve_forever())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
