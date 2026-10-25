from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
import re
import secrets
import shutil
import socket
import sys
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from filecompression import parallel
from filecompression.algorithms import algorithms
from filecompression.container import ContainerInfo, iter_decompress, pack_stream, read_info_path, unpack
from filecompression.container.format import MAX_ORIGINAL_SIZE, validate_filename
from filecompression.errors import CompressionError, OperationCancelled
from filecompression.recommend import recommend, recommend_file
from filecompression.transfer import TransferClient

try:
    from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
    from starlette.background import BackgroundTask
except ImportError:
    FastAPI = File = Form = HTTPException = Query = Request = UploadFile = CORSMiddleware = None
    FileResponse = JSONResponse = PlainTextResponse = Response = StreamingResponse = BackgroundTask = None


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


GIB = 1024 ** 3
AUTO_ALGORITHM = "auto"
CONTAINER_SUFFIX = ".fcmp"
ORIGINAL_SUFFIX = ".orig"
MAX_UPLOAD_SIZE = min(_env_int("FILECOMP_MAX_UPLOAD_BYTES", 4 * GIB), MAX_ORIGINAL_SIZE)
TRANSFER_TTL_SECONDS = _env_int("FILECOMP_TRANSFER_TTL_SECONDS", 30 * 60)
MAX_RECOMMEND_SAMPLE = 4 * 1024 * 1024
MAX_TRANSFERS = _env_int("FILECOMP_MAX_TRANSFERS", 32)
MAX_TRANSFER_BYTES = _env_int("FILECOMP_MAX_TRANSFER_BYTES", 16 * GIB)
MIN_FREE_BYTES = _env_int("FILECOMP_MIN_FREE_BYTES", 512 * 1024 * 1024)
MAX_CONCURRENT_UPLOADS = _env_int("FILECOMP_MAX_CONCURRENT_UPLOADS", 2)
MAX_CONCURRENT_COMPRESSIONS = _env_int("FILECOMP_MAX_CONCURRENT_COMPRESSIONS", 2)
# Keep the uploaded file next to its archive so downloads are served without decompression.
KEEP_ORIGINAL = os.getenv("FILECOMP_KEEP_ORIGINAL", "true").lower() == "true"
ENABLE_TRANSFER_SESSIONS = os.getenv("FILECOMP_ENABLE_SESSIONS", "false").lower() == "true"
TRANSFER_SERVER_HOST = os.getenv("FILECOMP_TRANSFER_HOST", "")
STORAGE_DIR = Path(os.getenv("FILECOMP_STORAGE_DIR", str(Path.cwd() / "data")))
API_KEY = os.getenv("FILECOMP_API_KEY", "")
RATE_LIMIT_WINDOW_SECONDS = _env_int("FILECOMP_RATE_LIMIT_WINDOW_SECONDS", 60)
RATE_LIMIT_REQUESTS = _env_int("FILECOMP_RATE_LIMIT_REQUESTS", 120)
PURGE_INTERVAL_SECONDS = 60
WRITE_BUFFER = 1024 * 1024
logger = logging.getLogger("filecompression.web")
upload_slots = asyncio.Semaphore(MAX_CONCURRENT_UPLOADS)
rate_limit: dict[str, tuple[int, float]] = {}
metrics = {"uploads_started": 0, "uploads_completed": 0, "uploads_failed": 0, "uploads_cancelled": 0}


@dataclass
class Transfer:
    identifier: str
    container_path: Path
    created_at: float
    info: ContainerInfo
    downloaded: bool = False
    compression_time: float | None = None
    recommended_algorithm: str | None = None
    original_path: Path | None = None

    @property
    def expired(self) -> bool:
        return time.time() - self.created_at > TRANSFER_TTL_SECONDS

    @property
    def stored_size(self) -> int:
        return self.info.compressed_size + (self.info.original_size if self.original_path else 0)


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
    recommended_algorithm: str | None = None
    compression_time: float | None = None
    created_at: float = field(default_factory=time.time)
    cancel_event: threading.Event = field(default_factory=threading.Event)

    @property
    def finished(self) -> bool:
        return self.status in {"completed", "cancelled", "error"}


transfers: dict[str, Transfer] = {}
sessions: dict[str, TransferClient] = {}
operations: dict[str, CompressionOperation] = {}
operation_lock = threading.RLock()
compression_executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_COMPRESSIONS, thread_name_prefix="filecompression")


# ---------------------------------------------------------------- storage

def _purge_expired() -> None:
    now = time.time()
    with operation_lock:
        expired = [(identifier, transfer) for identifier, transfer in transfers.items() if transfer.expired]
        for identifier, _ in expired:
            del transfers[identifier]
        for identifier, operation in list(operations.items()):
            # Running operations are kept no matter how long a large file takes.
            if operation.finished and now - operation.created_at > TRANSFER_TTL_SECONDS:
                operations.pop(identifier, None)
    for identifier, transfer in expired:
        # On POSIX a download that is still streaming keeps its open handle valid.
        transfer.container_path.unlink(missing_ok=True)
        if transfer.original_path is not None:
            transfer.original_path.unlink(missing_ok=True)
        logger.info("expired transfer id=%s", identifier)


def _stored_bytes() -> int:
    with operation_lock:
        return sum(item.stored_size for item in transfers.values())


def _free_bytes() -> int:
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(STORAGE_DIR).free


def _ensure_disk_space(needed: int) -> None:
    if _free_bytes() - needed < MIN_FREE_BYTES:
        raise HTTPException(507, "Not enough free disk space on the server")


def _register_transfer(identifier: str, temporary: Path, info: ContainerInfo, *, original: Path | None = None, compression_time: float | None = None, recommended: str | None = None) -> Transfer:
    """Move a finished container (and optionally the original file) into place and publish it, enforcing the storage quota."""
    _purge_expired()
    with operation_lock:
        stored = _stored_bytes()
        if len(transfers) >= MAX_TRANSFERS or stored + info.compressed_size > MAX_TRANSFER_BYTES:
            temporary.unlink(missing_ok=True)
            raise HTTPException(507, "Transfer storage limit reached")
        path = STORAGE_DIR / f"{identifier}{CONTAINER_SUFFIX}"
        temporary.replace(path)
        original_path = None
        # The original is a convenience: when the quota has no room for it, downloads fall back to decompression.
        if original is not None and stored + info.compressed_size + info.original_size <= MAX_TRANSFER_BYTES:
            original_path = STORAGE_DIR / f"{identifier}{ORIGINAL_SUFFIX}"
            original.replace(original_path)
        transfer = Transfer(identifier, path, time.time(), info, compression_time=compression_time, recommended_algorithm=recommended, original_path=original_path)
        transfers[identifier] = transfer
    return transfer


def _cleanup_partial_files() -> None:
    """Remove uploads and containers left behind by a crash or power loss."""
    for pattern in ("*.tmp", "*.part"):
        for path in STORAGE_DIR.glob(pattern):
            path.unlink(missing_ok=True)
    spool = STORAGE_DIR / "tmp"
    if spool.is_dir():
        for path in spool.iterdir():
            if path.is_file():
                path.unlink(missing_ok=True)


def _load_persisted_transfers() -> None:
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_partial_files()
    for path in STORAGE_DIR.glob(f"*{CONTAINER_SUFFIX}"):
        try:
            info = read_info_path(path)
        except (CompressionError, OSError):
            path.unlink(missing_ok=True)
            continue
        transfers[path.stem] = Transfer(path.stem, path, path.stat().st_mtime, info)
    for path in STORAGE_DIR.glob(f"*{ORIGINAL_SUFFIX}"):
        transfer = transfers.get(path.stem)
        if transfer is None or path.stat().st_size != transfer.info.original_size:
            path.unlink(missing_ok=True)
        else:
            transfer.original_path = path


# ---------------------------------------------------------------- uploads

async def _write_stream(chunks: AsyncIterator[bytes]) -> Path:
    """Stream an upload to disk in 1 MiB writes, enforcing the size limit while it arrives."""
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    path = STORAGE_DIR / f"upload-{uuid.uuid4().hex}.tmp"
    total = 0
    buffer = bytearray()
    try:
        with path.open("wb") as handle:
            async for chunk in chunks:
                total += len(chunk)
                if total > MAX_UPLOAD_SIZE:
                    raise HTTPException(413, "File exceeds the configured upload limit")
                buffer += chunk
                if len(buffer) >= WRITE_BUFFER:
                    await asyncio.to_thread(handle.write, bytes(buffer))
                    buffer.clear()
            if buffer:
                await asyncio.to_thread(handle.write, bytes(buffer))
        return path
    except BaseException:
        path.unlink(missing_ok=True)
        raise


async def _multipart_chunks(file: UploadFile) -> AsyncIterator[bytes]:
    while chunk := await file.read(WRITE_BUFFER):
        yield chunk


def _declared_length(request: Request) -> int | None:
    try:
        return int(request.headers["content-length"])
    except (KeyError, ValueError):
        return None


async def _receive_upload(request: Request, chunks: AsyncIterator[bytes]) -> Path:
    declared = _declared_length(request)
    if declared is not None:
        if declared > MAX_UPLOAD_SIZE + 64 * 1024:
            raise HTTPException(413, "File exceeds the configured upload limit")
        _ensure_disk_space(declared)
    async with upload_slots:
        return await _write_stream(chunks)


def _validated_filename(filename: str | None) -> str:
    try:
        return validate_filename(filename or "upload.bin")
    except CompressionError as error:
        raise HTTPException(400, str(error)) from error


def _validated_algorithm(algorithm: str) -> str:
    algorithm = algorithm.lower()
    if algorithm != AUTO_ALGORITHM and algorithm not in algorithms():
        raise HTTPException(400, f"Unknown algorithm: {algorithm}")
    return algorithm


# ---------------------------------------------------------------- compression

def _compress_upload(upload_path: Path, filename: str, algorithm: str, recommended: str, *, progress=None, cancelled=None) -> Transfer:
    identifier = secrets.token_urlsafe(8)
    temporary = STORAGE_DIR / f"{identifier}.part"
    try:
        started = time.perf_counter()
        with upload_path.open("rb") as source, temporary.open("wb") as destination:
            info = pack_stream(source, destination, filename, algorithm, progress=progress, cancelled=cancelled, executor=parallel.shared_executor())
        compression_time = time.perf_counter() - started
        original = None
        if KEEP_ORIGINAL:
            # Served directly later without a checksum pass, so it must survive a power loss intact.
            with upload_path.open("rb") as handle:
                os.fsync(handle.fileno())
            original = upload_path
        return _register_transfer(identifier, temporary, info, original=original, compression_time=compression_time, recommended=recommended)
    finally:
        temporary.unlink(missing_ok=True)


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
        "recommendedAlgorithm": operation.recommended_algorithm,
        "compressionTime": operation.compression_time,
    }


def _run_compression(operation: CompressionOperation, upload_path: Path) -> None:
    started = time.perf_counter()

    def progress(processed: int, total: int) -> None:
        elapsed = time.perf_counter() - started
        with operation_lock:
            operation.processed_bytes = processed
            operation.speed = processed / elapsed if elapsed > 0 else 0.0
            operation.eta = (total - processed) / operation.speed if operation.speed > 0 else None

    try:
        with operation_lock:
            if operation.cancel_event.is_set():
                raise OperationCancelled("Compression cancelled")
            operation.status = "processing"
        recommended = recommend_file(upload_path).choice
        algorithm = recommended if operation.algorithm == AUTO_ALGORITHM else operation.algorithm
        with operation_lock:
            operation.recommended_algorithm = recommended
            operation.algorithm = algorithm
        started = time.perf_counter()
        transfer = _compress_upload(upload_path, operation.filename, algorithm, recommended, progress=progress, cancelled=operation.cancel_event.is_set)
        with operation_lock:
            operation.transfer_id = transfer.identifier
            operation.processed_bytes = operation.total_bytes
            operation.eta = 0.0
            operation.compression_time = transfer.compression_time
            operation.status = "completed"
        metrics["uploads_completed"] += 1
        logger.info("compression finished id=%s transfer=%s %d -> %d bytes in %.1fs", operation.identifier, transfer.identifier, transfer.info.original_size, transfer.info.compressed_size, transfer.compression_time)
    except OperationCancelled as error:
        with operation_lock:
            operation.status = "cancelled"
            operation.error = str(error)
        metrics["uploads_cancelled"] += 1
    except Exception as error:
        message = error.detail if isinstance(error, HTTPException) else str(error)
        with operation_lock:
            operation.status = "error"
            operation.error = message
        logger.error("compression failed id=%s: %s", operation.identifier, message)
        metrics["uploads_failed"] += 1
    finally:
        upload_path.unlink(missing_ok=True)


def _start_operation(upload_path: Path, filename: str, algorithm: str) -> dict:
    operation = CompressionOperation(uuid.uuid4().hex, filename, upload_path.stat().st_size, algorithm)
    with operation_lock:
        operations[operation.identifier] = operation
    metrics["uploads_started"] += 1
    logger.info("compression queued id=%s filename=%s size=%d algorithm=%s", operation.identifier, filename, operation.total_bytes, algorithm)
    compression_executor.submit(_run_compression, operation, upload_path)
    return _operation_metadata(operation)


# ---------------------------------------------------------------- downloads

def _restore_chunks(path: Path, on_complete=None, *, remove: bool = False) -> Iterator[bytes]:
    """Stream the restored file; a checksum failure aborts the response so the client sees a failed download."""
    try:
        with path.open("rb") as handle:
            yield from iter_decompress(handle, executor=parallel.shared_executor())
        if on_complete is not None:
            on_complete()
    finally:
        if remove:
            path.unlink(missing_ok=True)


def _attachment(filename: str) -> str:
    fallback = filename.encode("ascii", "replace").decode("ascii").replace("?", "_").replace('"', "_")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"


def _compressed_name(filename: str) -> str:
    return f"{filename}{CONTAINER_SUFFIX}"


def _metadata(identifier: str, transfer: Transfer) -> dict:
    info = transfer.info
    return {
        "id": identifier,
        "filename": info.filename,
        "algorithm": info.algorithm,
        "original_size": info.original_size,
        "compressed_size": info.compressed_size,
        "compression_ratio": info.original_size / info.compressed_size if info.compressed_size else 0,
        "space_saving": (1 - info.compressed_size / info.original_size) * 100 if info.original_size else 0,
        "checksum": info.checksum,
        "status": "downloaded" if transfer.downloaded else "ready",
        "expires_in": max(0, int(TRANSFER_TTL_SECONDS - (time.time() - transfer.created_at))),
        "compression_time": transfer.compression_time,
        "recommended_algorithm": transfer.recommended_algorithm,
    }


def _get_transfer(identifier: str) -> Transfer:
    _purge_expired()
    transfer = transfers.get(identifier)
    if transfer is None:
        raise HTTPException(404, "Transfer not found or expired")
    return transfer


# ---------------------------------------------------------------- local network discovery

_VIRTUAL_INTERFACE = re.compile(r"^(lo|docker|br-|veth|virbr|cni|flannel|tailscale|zt|tun|tap)")


def _interface_addresses() -> list[str]:
    """IPv4 addresses of physical interfaces (Linux), e.g. eth0 / wlan0 on a Raspberry Pi."""
    if not sys.platform.startswith("linux"):
        return []
    import fcntl
    import struct

    addresses = []
    for _, name in socket.if_nameindex():
        if _VIRTUAL_INTERFACE.match(name):
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            try:
                packed = fcntl.ioctl(sock.fileno(), 0x8915, struct.pack("256s", name[:15].encode()))  # SIOCGIFADDR
            except OSError:
                continue
        addresses.append(socket.inet_ntoa(packed[20:24]))
    return addresses


def _route_address() -> str | None:
    """Source address of the default route; no packet is sent. Works without internet access."""
    for probe in ("8.8.8.8", "192.168.0.1", "10.0.0.1"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((probe, 80))
                address = sock.getsockname()[0]
                if not ipaddress.ip_address(address).is_loopback:
                    return address
        except OSError:
            continue
    return None


def _lan_addresses() -> list[str]:
    candidates = [address for address in (_route_address(), *_interface_addresses()) if address]
    unique = list(dict.fromkeys(candidates))
    # Private LAN ranges first; the 172.16/12 range is also where Docker bridges live, so it goes last.
    def rank(address: str) -> int:
        ip = ipaddress.ip_address(address)
        if ip in ipaddress.ip_network("192.168.0.0/16") or ip in ipaddress.ip_network("10.0.0.0/8"):
            return 0
        if ip in ipaddress.ip_network("172.16.0.0/12"):
            return 2
        return 1
    return sorted(unique, key=rank)


def _network_ip() -> str:
    addresses = _lan_addresses()
    return addresses[0] if addresses else "127.0.0.1"


def _mdns_hostname() -> str | None:
    hostname = os.getenv("FILECOMP_MDNS_HOSTNAME", "").strip() or socket.gethostname().split(".")[0]
    # A bridged Docker container gets a random 12-hex-digit hostname that nobody can resolve.
    if not hostname or re.fullmatch(r"[0-9a-f]{12}", hostname) or hostname == "localhost":
        return None
    return f"{hostname}.local"


def _is_routable_literal(host: str | None) -> bool:
    try:
        address = ipaddress.ip_address((host or "").strip("[]"))
    except ValueError:
        return False
    return not (address.is_loopback or address.is_unspecified)


def network_details(request_hostname: str | None = None, request_origin: str | None = None) -> dict:
    port = _env_int("FILECOMP_PUBLIC_PORT", _env_int("FILECOMP_WEB_PORT", 8000))
    addresses = _lan_addresses()
    urls = [f"http://{address}:{port}" for address in addresses]
    mdns = _mdns_hostname()
    if mdns:
        urls.append(f"http://{mdns}:{port}")
    public_url = os.getenv("FILECOMP_PUBLIC_URL", "").strip().rstrip("/")
    public_host = os.getenv("FILECOMP_PUBLIC_HOST", "").strip()
    if public_url:
        url = public_url
    elif public_host:
        url = f"http://{public_host}:{port}"
    elif request_origin and _is_routable_literal(request_hostname):
        # The browser already reached the server at this IP address, so other LAN devices can too.
        url = request_origin
    else:
        url = urls[0] if urls else f"http://127.0.0.1:{port}"
    host = url.split("://", 1)[-1].rsplit(":", 1)[0]
    if url not in urls:
        urls.insert(0, url)
    return {"host": host, "port": port, "url": url, "urls": urls, "mdns": mdns}


# ---------------------------------------------------------------- application

def create_app():
    if FastAPI is None:
        raise RuntimeError("Install web dependencies with: pip install -e '.[web]'")

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async def purge_loop() -> None:
            while True:
                await asyncio.sleep(PURGE_INTERVAL_SECONDS)
                try:
                    await asyncio.to_thread(_purge_expired)
                except Exception:  # never let housekeeping kill the server
                    logger.exception("purge failed")

        details = network_details()
        logger.info("FileComp is reachable on the local network at: %s", ", ".join(details["urls"]))
        if Path("/.dockerenv").exists() and details["host"].startswith("172."):
            logger.warning("Running inside a bridged Docker network; use network_mode: host or set FILECOMP_PUBLIC_HOST so share links use the LAN address")
        task = asyncio.create_task(purge_loop())
        try:
            yield
        finally:
            task.cancel()
            parallel.shutdown()

    app = FastAPI(title="FileComp API", version="2.0", lifespan=lifespan)
    allowed_origins = [item.strip() for item in os.getenv("FILECOMP_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if item.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=allowed_origins, allow_methods=["GET", "POST", "DELETE"], allow_headers=["Content-Type", "X-API-Key"], expose_headers=["Content-Disposition", "X-Checksum"], max_age=600)

    _load_persisted_transfers()

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    @app.middleware("http")
    async def production_guards(request, call_next):
        # Only state-changing requests count, so progress polling during a long compression is never throttled.
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            client = request.client.host if request.client else "unknown"
            now = time.monotonic()
            count, window = rate_limit.get(client, (0, now))
            if now - window >= RATE_LIMIT_WINDOW_SECONDS:
                count, window = 0, now
            count += 1
            rate_limit[client] = (count, window)
            if count > RATE_LIMIT_REQUESTS:
                return JSONResponse({"detail": "Rate limit exceeded"}, status_code=429, headers={"Retry-After": str(RATE_LIMIT_WINDOW_SECONDS)})
        if API_KEY and request.url.path.startswith("/api/") and request.url.path != "/api/health":
            if request.headers.get("x-api-key") != API_KEY:
                return JSONResponse({"detail": "Authentication required"}, status_code=401)
        return await call_next(request)

    frontend_index = Path(os.getenv("FILECOMP_FRONTEND_DIR", str(Path(__file__).resolve().parents[3] / "frontend" / "dist"))) / "index.html"

    def active_operations() -> int:
        return sum(operation.status in {"queued", "processing", "cancelling"} for operation in operations.values())

    @app.get("/api/health")
    async def health() -> dict:
        return {
            "status": "online",
            "active_operations": active_operations(),
            "stored_transfers": len(transfers),
            "stored_bytes": _stored_bytes(),
            "free_bytes": _free_bytes(),
            "workers": parallel.configured_workers(),
            "max_upload_bytes": MAX_UPLOAD_SIZE,
        }

    @app.get("/metrics", include_in_schema=False)
    async def metrics_endpoint() -> PlainTextResponse:
        lines = [f"filecompression_{key} {value}" for key, value in metrics.items()]
        lines.append(f"filecompression_active_operations {active_operations()}")
        lines.append(f"filecompression_stored_transfers {len(transfers)}")
        lines.append(f"filecompression_stored_bytes {_stored_bytes()}")
        lines.append(f"filecompression_free_bytes {_free_bytes()}")
        return PlainTextResponse("\n".join(lines) + "\n")

    @app.get("/api/network-info")
    async def network_info(request: Request) -> dict:
        details = await asyncio.to_thread(network_details, request.url.hostname, f"{request.url.scheme}://{request.url.netloc}")
        return {**details, "maxUploadBytes": MAX_UPLOAD_SIZE}

    @app.get("/api/algorithms")
    async def list_algorithms() -> list[dict]:
        descriptions = {"huffman": "Frequency-based lossless compression.", "lzw": "Dictionary-based lossless compression.", "rle": "Run-length encoding for repetitive data.", "stored": "Original bytes when compression would increase size."}
        return [{"id": name, "label": name.upper(), "description": descriptions[name]} for name in algorithms()]

    @app.post("/api/recommend")
    async def recommend_algorithm(file: UploadFile = File(...)) -> dict:
        data = await file.read(MAX_RECOMMEND_SAMPLE + 1)
        if len(data) > MAX_RECOMMEND_SAMPLE:
            raise HTTPException(413, "Sample is too large")
        return (await asyncio.to_thread(recommend, data)).as_dict()

    @app.post("/api/compress")
    async def start_compression(request: Request, file: UploadFile = File(...), algorithm: str = Form(AUTO_ALGORITHM)) -> dict:
        filename = _validated_filename(file.filename)
        algorithm = _validated_algorithm(algorithm)
        upload_path = await _receive_upload(request, _multipart_chunks(file))
        return _start_operation(upload_path, filename, algorithm)

    @app.post("/api/compress/stream")
    async def start_stream_compression(request: Request, filename: str = Query(...), algorithm: str = Query(AUTO_ALGORITHM)) -> dict:
        """Raw request body upload: written straight to disk once, without multipart spooling."""
        filename = _validated_filename(filename)
        algorithm = _validated_algorithm(algorithm)
        upload_path = await _receive_upload(request, request.stream())
        return _start_operation(upload_path, filename, algorithm)

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
    async def create_transfer(request: Request, file: UploadFile = File(...), algorithm: str = Form(...)) -> dict:
        filename = _validated_filename(file.filename)
        algorithm = _validated_algorithm(algorithm)
        upload_path = await _receive_upload(request, _multipart_chunks(file))
        try:
            recommended = (await asyncio.to_thread(recommend_file, upload_path)).choice
            chosen = recommended if algorithm == AUTO_ALGORITHM else algorithm
            transfer = await asyncio.to_thread(_compress_upload, upload_path, filename, chosen, recommended)
        except (CompressionError, ValueError) as error:
            raise HTTPException(400, str(error)) from error
        finally:
            upload_path.unlink(missing_ok=True)
        return _metadata(transfer.identifier, transfer)

    @app.post("/api/archives")
    async def import_archive(request: Request, filename: str = Query("archive.fcmp")) -> dict:
        """Store an uploaded .fcmp as a transfer so its restored file can be downloaded as a stream."""
        upload_path = await _receive_upload(request, request.stream())
        try:
            info = await asyncio.to_thread(read_info_path, upload_path)
            identifier = secrets.token_urlsafe(8)
            transfer = await asyncio.to_thread(_register_transfer, identifier, upload_path, info)
        except CompressionError as error:
            raise HTTPException(422, f"Not a valid FCMP archive: {error}") from error
        finally:
            upload_path.unlink(missing_ok=True)
        logger.info("archive imported id=%s source=%s", transfer.identifier, filename)
        return _metadata(transfer.identifier, transfer)

    @app.get("/api/transfers/{identifier}")
    async def transfer_info(identifier: str) -> dict:
        return _metadata(identifier, _get_transfer(identifier))

    @app.get("/api/transfers/{identifier}/download")
    async def download(identifier: str, compressed: bool = False) -> Response:
        """Return the original file (restored and SHA-256 checked while streaming) or, with ?compressed=true, the FCMP archive."""
        transfer = _get_transfer(identifier)
        info = transfer.info
        if compressed:
            transfer.downloaded = True
            return FileResponse(transfer.container_path, media_type="application/octet-stream", headers={"Content-Disposition": _attachment(_compressed_name(info.filename)), "X-Checksum": info.checksum})

        def mark_downloaded() -> None:
            transfer.downloaded = True

        if transfer.original_path is not None:
            # The kept upload is the same data the archive's SHA-256 was computed from.
            headers = {"Content-Disposition": _attachment(info.filename), "X-Checksum": info.checksum}
            return FileResponse(transfer.original_path, media_type="application/octet-stream", headers=headers, background=BackgroundTask(mark_downloaded))
        headers = {"Content-Disposition": _attachment(info.filename), "Content-Length": str(info.original_size), "X-Checksum": info.checksum}
        return StreamingResponse(_restore_chunks(transfer.container_path, mark_downloaded), media_type="application/octet-stream", headers=headers)

    @app.post("/api/decompress")
    async def decompress_upload(request: Request, file: UploadFile = File(...)) -> Response:
        upload_path = await _receive_upload(request, _multipart_chunks(file))
        try:
            info = await asyncio.to_thread(read_info_path, upload_path)
        except CompressionError as error:
            upload_path.unlink(missing_ok=True)
            raise HTTPException(422, f"Not a valid FCMP archive: {error}") from error
        headers = {"Content-Disposition": _attachment(info.filename), "Content-Length": str(info.original_size), "X-Checksum": info.checksum}
        return StreamingResponse(_restore_chunks(upload_path, remove=True), media_type="application/octet-stream", headers=headers)

    @app.post("/api/transfers/{identifier}/verify")
    async def verify(identifier: str) -> dict:
        transfer = _get_transfer(identifier)

        def restored_size() -> int:
            return sum(len(chunk) for chunk in _restore_chunks(transfer.container_path))

        try:
            size = await asyncio.to_thread(restored_size)
        except CompressionError as error:
            raise HTTPException(422, str(error)) from error
        return {"verified": True, "checksum": transfer.info.checksum, "size": size}

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
    async def send_session(request: Request, identifier: str, recipient: str = Form(...), file: UploadFile = File(...)) -> dict:
        if not ENABLE_TRANSFER_SESSIONS:
            raise HTTPException(404, "Transfer sessions are disabled")
        client = sessions.get(identifier)
        if client is None:
            raise HTTPException(404, "Session not found")
        filename = _validated_filename(file.filename)
        upload_path = await _receive_upload(request, _multipart_chunks(file))
        try:
            from filecompression.container import pack

            data = upload_path.read_bytes()
            container = await asyncio.to_thread(pack, data, filename, "huffman")
            await client.send(recipient, container)
        except (CompressionError, OSError, ConnectionError, TimeoutError) as error:
            raise HTTPException(502, str(error)) from error
        finally:
            upload_path.unlink(missing_ok=True)
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
        return Response(container, media_type="application/octet-stream", headers={"Content-Disposition": _attachment(_compressed_name(metadata.filename))})

    if frontend_index.exists():
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
