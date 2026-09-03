from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
import secrets
import socket
import time
from dataclasses import dataclass
from pathlib import Path

from filecompression.algorithms import algorithms
from filecompression.algorithms.base import MAX_DECOMPRESSED_SIZE
from filecompression.container import pack, unpack
from filecompression.errors import CompressionError
from filecompression.transfer import TransferClient

try:
    from fastapi import FastAPI, File, Form, HTTPException, UploadFile
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import Response
except ImportError:
    FastAPI = File = Form = HTTPException = UploadFile = CORSMiddleware = Response = None

MAX_UPLOAD_SIZE = MAX_DECOMPRESSED_SIZE
TRANSFER_TTL_SECONDS = 30 * 60


@dataclass
class Transfer:
    identifier: str
    container: bytes
    created_at: float
    downloaded: bool = False

    @property
    def expired(self) -> bool:
        return time.time() - self.created_at > TRANSFER_TTL_SECONDS


transfers: dict[str, Transfer] = {}
sessions: dict[str, TransferClient] = {}


def _purge_expired() -> None:
    for identifier, transfer in list(transfers.items()):
        if transfer.expired:
            del transfers[identifier]


def _network_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def _metadata(identifier: str, transfer: Transfer) -> dict:
    container = unpack(transfer.container)
    return {
        "id": identifier,
        "filename": container.filename,
        "algorithm": container.algorithm,
        "original_size": container.original_size,
        "compressed_size": container.compressed_size,
        "compression_ratio": container.original_size / container.compressed_size if container.compressed_size else 0,
        "space_saving": (1 - container.compressed_size / container.original_size) * 100 if container.original_size else 0,
        "checksum": container.checksum,
        "status": "downloaded" if transfer.downloaded else "ready",
        "expires_in": max(0, int(TRANSFER_TTL_SECONDS - (time.time() - transfer.created_at))),
    }


def create_app():
    if FastAPI is None:
        raise RuntimeError("Install web dependencies with: pip install -e '.[web]'")

    app = FastAPI(title="FileComp API", version="1.0")
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"], allow_methods=["*"], allow_headers=["*"])

    frontend_index = Path(__file__).resolve().parents[3] / "frontend" / "dist" / "index.html"

    @app.get("/api/health")
    async def health() -> dict:
        return {"status": "online"}

    @app.get("/api/network-info")
    async def network_info() -> dict:
        host = _network_ip()
        port = int(os.getenv("FILECOMP_WEB_PORT", "8000"))
        return {"host": host, "port": port, "url": f"http://{host}:{port}"}

    @app.get("/api/algorithms")
    async def list_algorithms() -> list[dict]:
        descriptions = {"huffman": "Frequency-based lossless compression.", "lzw": "Dictionary-based lossless compression.", "rle": "Run-length encoding for repetitive data."}
        return [{"id": name, "label": name.upper(), "description": descriptions[name]} for name in algorithms()]

    @app.post("/api/compress")
    async def compress(file: UploadFile = File(...), algorithm: str = Form(...)) -> Response:
        data = await file.read(MAX_UPLOAD_SIZE + 1)
        if len(data) > MAX_UPLOAD_SIZE:
            raise HTTPException(413, "File exceeds the 256 MiB limit")
        try:
            container = await asyncio.to_thread(pack, data, Path(file.filename or "upload.bin").name, algorithm)
        except (CompressionError, ValueError) as error:
            raise HTTPException(400, str(error)) from error
        metadata = unpack(container)
        return Response(container, media_type="application/octet-stream", headers={"X-File-Metadata": str(_metadata("", Transfer("", container, time.time())))})

    @app.post("/api/transfers")
    async def create_transfer(file: UploadFile = File(...), algorithm: str = Form(...)) -> dict:
        data = await file.read(MAX_UPLOAD_SIZE + 1)
        if len(data) > MAX_UPLOAD_SIZE:
            raise HTTPException(413, "File exceeds the 256 MiB limit")
        try:
            container = await asyncio.to_thread(pack, data, Path(file.filename or "upload.bin").name, algorithm)
        except (CompressionError, ValueError) as error:
            raise HTTPException(400, str(error)) from error
        _purge_expired()
        identifier = secrets.token_urlsafe(8)
        transfers[identifier] = Transfer(identifier, container, time.time())
        return _metadata(identifier, transfers[identifier])

    @app.get("/api/transfers/{identifier}")
    async def transfer_info(identifier: str) -> dict:
        _purge_expired()
        transfer = transfers.get(identifier)
        if transfer is None:
            raise HTTPException(404, "Transfer not found or expired")
        return _metadata(identifier, transfer)

    @app.get("/api/transfers/{identifier}/download")
    async def download(identifier: str) -> Response:
        _purge_expired()
        transfer = transfers.get(identifier)
        if transfer is None:
            raise HTTPException(404, "Transfer not found or expired")
        try:
            container = unpack(transfer.container)
        except CompressionError as error:
            raise HTTPException(422, str(error)) from error
        transfer.downloaded = True
        return Response(transfer.container, media_type="application/octet-stream", headers={"Content-Disposition": f'attachment; filename="{container.filename}.fcmp"', "X-Checksum": container.checksum})

    @app.post("/api/transfers/{identifier}/verify")
    async def verify(identifier: str) -> dict:
        _purge_expired()
        transfer = transfers.get(identifier)
        if transfer is None:
            raise HTTPException(404, "Transfer not found or expired")
        try:
            restored = await asyncio.to_thread(unpack(transfer.container).decompress)
        except CompressionError as error:
            raise HTTPException(422, str(error)) from error
        return {"verified": True, "checksum": hashlib.sha256(restored).hexdigest(), "size": len(restored)}

    @app.post("/api/sessions")
    async def create_session(host: str = Form(...), port: int = Form(...), user: str = Form(...)) -> dict:
        if not user.isidentifier() or not 1 <= len(user) <= 64 or not 1 <= port <= 65535:
            raise HTTPException(400, "Invalid user or port")
        client = TransferClient(host, port, user)
        try:
            await client.connect()
        except (OSError, ConnectionError, TimeoutError) as error:
            raise HTTPException(502, "Unable to connect to transfer server") from error
        identifier = secrets.token_urlsafe(8)
        sessions[identifier] = client
        return {"id": identifier, "user": user, "status": "connected"}

    @app.delete("/api/sessions/{identifier}")
    async def close_session(identifier: str) -> dict:
        client = sessions.pop(identifier, None)
        if client:
            await client.close()
        return {"status": "closed"}

    @app.post("/api/sessions/{identifier}/send")
    async def send_session(identifier: str, recipient: str = Form(...), file: UploadFile = File(...)) -> dict:
        client = sessions.get(identifier)
        if client is None:
            raise HTTPException(404, "Session not found")
        data = await file.read(MAX_UPLOAD_SIZE + 1)
        if len(data) > MAX_UPLOAD_SIZE:
            raise HTTPException(413, "File exceeds the 256 MiB limit")
        try:
            container = await asyncio.to_thread(pack, data, Path(file.filename or "upload.bin").name, "huffman")
            await client.send(recipient, container)
        except (CompressionError, OSError, ConnectionError, TimeoutError) as error:
            raise HTTPException(502, str(error)) from error
        return {"status": "sent", "compressed_size": len(container)}

    @app.post("/api/sessions/{identifier}/receive")
    async def receive_session(identifier: str) -> Response:
        client = sessions.get(identifier)
        if client is None:
            raise HTTPException(404, "Session not found")
        try:
            container = await client.receive()
            metadata = unpack(container)
        except (CompressionError, FileNotFoundError, OSError, ConnectionError, TimeoutError) as error:
            raise HTTPException(502, str(error)) from error
        return Response(container, media_type="application/octet-stream", headers={"Content-Disposition": f'attachment; filename="{metadata.filename}.fcmp"'})

    if frontend_index.exists():
        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles

        app.mount("/assets", StaticFiles(directory=frontend_index.parent / "assets"), name="assets")

        @app.get("/", include_in_schema=False)
        async def frontend_home() -> FileResponse:
            return FileResponse(frontend_index)

        @app.get("/share/{identifier}", include_in_schema=False)
        async def frontend_share(identifier: str) -> FileResponse:
            return FileResponse(frontend_index)

    return app


app = create_app()
