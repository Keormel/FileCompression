from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import os
import secrets
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from filecompression.algorithms import algorithms
from filecompression.algorithms.base import MAX_DECOMPRESSED_SIZE
from filecompression.container import pack, unpack
from filecompression.container.format import validate_filename
from filecompression.errors import CompressionError
from filecompression.transfer import TransferClient

try:
    from fastapi import FastAPI, File, Form, HTTPException, UploadFile
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import Response
except ImportError:
    FastAPI = File = Form = HTTPException = UploadFile = CORSMiddleware = Response = None

MAX_UPLOAD_SIZE = min(int(os.getenv("FILECOMP_MAX_UPLOAD_BYTES", str(MAX_DECOMPRESSED_SIZE))), MAX_DECOMPRESSED_SIZE)
TRANSFER_TTL_SECONDS = 30 * 60
MAX_TRANSFERS = int(os.getenv("FILECOMP_MAX_TRANSFERS", "32"))
MAX_TRANSFER_BYTES = int(os.getenv("FILECOMP_MAX_TRANSFER_BYTES", str(1024 * 1024 * 1024)))
MAX_CONCURRENT_UPLOADS = int(os.getenv("FILECOMP_MAX_CONCURRENT_UPLOADS", "2"))
ENABLE_TRANSFER_SESSIONS = os.getenv("FILECOMP_ENABLE_SESSIONS", "false").lower() == "true"
TRANSFER_SERVER_HOST = os.getenv("FILECOMP_TRANSFER_HOST", "")
logger = logging.getLogger("filecompression.web")
upload_slots = asyncio.Semaphore(MAX_CONCURRENT_UPLOADS)


@dataclass
class Transfer:
    identifier: str
    container: bytes
    created_at: float
    downloaded: bool = False

    @property
    def expired(self) -> bool:
        return time.time() - self.created_at > TRANSFER_TTL_SECONDS


@dataclass
class CompressionOperation:
    identifier: str
    filename: str
    total_bytes: int
    algorithm: str
    processed_bytes: int = 0
    speed: float = 0.0
    eta: float | None = None
    status: str = "queued"
    error: str | None = None
    transfer_id: str | None = None
    created_at: float = field(default_factory=time.time)
    cancel_event: threading.Event = field(default_factory=threading.Event)


transfers: dict[str, Transfer] = {}
sessions: dict[str, TransferClient] = {}
operations: dict[str, CompressionOperation] = {}
operation_lock = threading.Lock()
compression_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="filecompression")


def _purge_expired() -> None:
    now = time.time()
    for identifier, transfer in list(transfers.items()):
        if transfer.expired:
            del transfers[identifier]
            logger.info("expired transfer id=%s", identifier)
    with operation_lock:
        for identifier, operation in list(operations.items()):
            if now - operation.created_at > TRANSFER_TTL_SECONDS:
                operations.pop(identifier, None)


async def _read_upload(file: UploadFile) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(min(1024 * 1024, MAX_UPLOAD_SIZE + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_UPLOAD_SIZE:
            raise HTTPException(413, "File exceeds the configured upload limit")
    return b"".join(chunks)


def _store_transfer(container: bytes) -> str:
    _purge_expired()
    stored_bytes = sum(len(item.container) for item in transfers.values())
    if len(transfers) >= MAX_TRANSFERS or stored_bytes + len(container) > MAX_TRANSFER_BYTES:
        raise HTTPException(507, "Transfer storage limit reached")
    identifier = secrets.token_urlsafe(8)
    transfers[identifier] = Transfer(identifier, container, time.time())
    return identifier


def _operation_metadata(operation: CompressionOperation) -> dict:
    return {
        "operationId": operation.identifier,
        "filename": operation.filename,
        "algorithm": operation.algorithm,
        "progress": round(operation.processed_bytes / operation.total_bytes * 100, 2) if operation.total_bytes else 100,
        "processedBytes": operation.processed_bytes,
        "totalBytes": operation.total_bytes,
        "speed": operation.speed,
        "eta": operation.eta,
        "status": operation.status,
        "error": operation.error,
        "transferId": operation.transfer_id,
    }


def _run_compression(operation: CompressionOperation, data: bytes) -> None:
    started = time.perf_counter()

    def progress(processed: int, total: int) -> None:
        if operation.cancel_event.is_set():
            raise RuntimeError("Compression cancelled")
        elapsed = time.perf_counter() - started
        with operation_lock:
            operation.processed_bytes = processed
            operation.speed = processed / elapsed if elapsed > 0 else 0.0
            operation.eta = (total - processed) / operation.speed if operation.speed > 0 else None

    try:
        with operation_lock:
            operation.status = "processing"
        container = pack(data, operation.filename, operation.algorithm, progress)
        if operation.cancel_event.is_set():
            raise RuntimeError("Compression cancelled")
        _purge_expired()
        with operation_lock:
            stored_bytes = sum(len(item.container) for item in transfers.values())
            if len(transfers) >= MAX_TRANSFERS or stored_bytes + len(container) > MAX_TRANSFER_BYTES:
                raise RuntimeError("Transfer storage limit reached")
            identifier = secrets.token_urlsafe(8)
            transfers[identifier] = Transfer(identifier, container, time.time())
            operation.transfer_id = identifier
            operation.processed_bytes = operation.total_bytes
            operation.eta = 0.0
            operation.status = "completed"
    except RuntimeError as error:
        with operation_lock:
            operation.status = "cancelled" if "cancelled" in str(error).lower() else "error"
            operation.error = str(error)
    except Exception as error:
        with operation_lock:
            operation.status = "error"
            operation.error = str(error)
            logger.error("compression failed id=%s: %s", operation.identifier, error)


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
    allowed_origins = [item.strip() for item in os.getenv("FILECOMP_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if item.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=allowed_origins, allow_methods=["GET", "POST", "DELETE"], allow_headers=["Content-Type"], max_age=600)

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

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
        descriptions = {"huffman": "Frequency-based lossless compression.", "lzw": "Dictionary-based lossless compression.", "rle": "Run-length encoding for repetitive data.", "stored": "Original bytes when compression would increase size."}
        return [{"id": name, "label": name.upper(), "description": descriptions[name]} for name in algorithms()]

    @app.post("/api/compress")
    async def start_compression(file: UploadFile = File(...), algorithm: str = Form(...)) -> dict:
        try:
            filename = validate_filename(file.filename or "upload.bin")
        except CompressionError as error:
            raise HTTPException(400, str(error)) from error
        async with upload_slots:
            data = await _read_upload(file)
        operation = CompressionOperation(
            uuid.uuid4().hex,
            filename,
            len(data),
            algorithm,
        )
        with operation_lock:
            operations[operation.identifier] = operation
        logger.info("compression queued id=%s filename=%s size=%d algorithm=%s", operation.identifier, filename, len(data), algorithm)
        compression_executor.submit(_run_compression, operation, data)
        return _operation_metadata(operation)

    @app.get("/api/compress/{identifier}/status")
    async def compression_status(identifier: str) -> dict:
        operation = operations.get(identifier)
        if operation is None:
            raise HTTPException(404, "Compression operation not found")
        with operation_lock:
            return _operation_metadata(operation)

    @app.post("/api/compress/{identifier}/cancel")
    async def cancel_compression(identifier: str) -> dict:
        operation = operations.get(identifier)
        if operation is None:
            raise HTTPException(404, "Compression operation not found")
        operation.cancel_event.set()
        with operation_lock:
            if operation.status in {"queued", "processing"}:
                operation.status = "cancelling"
        return _operation_metadata(operation)

    @app.post("/api/transfers")
    async def create_transfer(file: UploadFile = File(...), algorithm: str = Form(...)) -> dict:
        try:
            filename = validate_filename(file.filename or "upload.bin")
        except CompressionError as error:
            raise HTTPException(400, str(error)) from error
        async with upload_slots:
            data = await _read_upload(file)
        try:
            container = await asyncio.to_thread(pack, data, filename, algorithm)
        except (CompressionError, ValueError) as error:
            raise HTTPException(400, str(error)) from error
        identifier = _store_transfer(container)
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
            restored = await asyncio.to_thread(container.decompress)
        except CompressionError as error:
            raise HTTPException(422, str(error)) from error
        transfer.downloaded = True
        return Response(restored, media_type="application/octet-stream", headers={"Content-Disposition": f'attachment; filename="{container.filename}"', "X-Checksum": container.checksum})

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
        if not ENABLE_TRANSFER_SESSIONS:
            raise HTTPException(404, "Transfer sessions are disabled")
        if not TRANSFER_SERVER_HOST or host != TRANSFER_SERVER_HOST:
            raise HTTPException(403, "Transfer host is not allowed")
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
        if not ENABLE_TRANSFER_SESSIONS:
            raise HTTPException(404, "Transfer sessions are disabled")
        client = sessions.pop(identifier, None)
        if client:
            await client.close()
        return {"status": "closed"}

    @app.post("/api/sessions/{identifier}/send")
    async def send_session(identifier: str, recipient: str = Form(...), file: UploadFile = File(...)) -> dict:
        if not ENABLE_TRANSFER_SESSIONS:
            raise HTTPException(404, "Transfer sessions are disabled")
        client = sessions.get(identifier)
        if client is None:
            raise HTTPException(404, "Session not found")
        try:
            filename = validate_filename(file.filename or "upload.bin")
        except CompressionError as error:
            raise HTTPException(400, str(error)) from error
        async with upload_slots:
            data = await _read_upload(file)
        try:
            container = await asyncio.to_thread(pack, data, filename, "huffman")
            await client.send(recipient, container)
        except (CompressionError, OSError, ConnectionError, TimeoutError) as error:
            raise HTTPException(502, str(error)) from error
        return {"status": "sent", "compressed_size": len(container)}

    @app.post("/api/sessions/{identifier}/receive")
    async def receive_session(identifier: str) -> Response:
        if not ENABLE_TRANSFER_SESSIONS:
            raise HTTPException(404, "Transfer sessions are disabled")
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
