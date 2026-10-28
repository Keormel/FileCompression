"""Hotspot speed-ups and overload protection of the web API."""

import asyncio
import gzip
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

import filecompression.web.api as web_api
from filecompression.web.api import FrontendFiles, app, reservations, transfers


def _wait_for(client: TestClient, operation_id: str) -> dict:
    for _ in range(500):
        status = client.get(f"/api/compress/{operation_id}/status").json()
        if status["status"] in {"completed", "error", "cancelled"}:
            return status
        time.sleep(0.02)
    raise AssertionError("Compression operation did not finish")


def test_gzip_encoded_upload_is_inflated_and_restored_exactly() -> None:
    transfers.clear()
    client = TestClient(app)
    data = b"timestamp,sensor,value\n" + b"2026-10-28T12:00:00,t1,21.5\n" * 50_000
    body = gzip.compress(data)
    assert len(body) < len(data) // 10
    response = client.post("/api/compress/stream", params={"filename": "log.csv", "size": len(data), "encoding": "gzip"}, content=body)
    assert response.status_code == 200, response.text
    status = _wait_for(client, response.json()["operationId"])
    assert status["status"] == "completed", status
    assert status["totalBytes"] == len(data)
    assert client.get(f"/api/transfers/{status['transferId']}/download").content == data
    assert not reservations


def test_gzip_bomb_and_broken_gzip_are_rejected(monkeypatch) -> None:
    monkeypatch.setattr(web_api, "MAX_UPLOAD_SIZE", 1_000_000)
    client = TestClient(app)
    bomb = gzip.compress(b"\0" * 20_000_000)
    assert client.post("/api/compress/stream", params={"filename": "a.bin", "encoding": "gzip"}, content=bomb).status_code == 413
    assert client.post("/api/compress/stream", params={"filename": "a.bin", "encoding": "gzip"}, content=b"not gzip at all").status_code == 400
    truncated = gzip.compress(b"hello world" * 1000)[:-10]
    assert client.post("/api/compress/stream", params={"filename": "a.bin", "encoding": "gzip"}, content=truncated).status_code == 400
    assert client.post("/api/compress/stream", params={"filename": "a.bin", "encoding": "br"}, content=b"x").status_code == 400
    assert not reservations
    assert not list(web_api.STORAGE_DIR.glob("upload-*.tmp"))


def test_declared_size_over_the_limit_is_refused_before_the_body(monkeypatch) -> None:
    monkeypatch.setattr(web_api, "MAX_UPLOAD_SIZE", 1000)
    client = TestClient(app)
    assert client.post("/api/compress/stream", params={"filename": "a.bin", "size": 5000}, content=b"x").status_code == 413
    # Multipart is spooled by the framework, so the middleware refuses it from Content-Length alone.
    assert client.post("/api/compress", files={"file": ("a.bin", b"x" * 200_000)}).status_code == 413


def test_full_job_queue_answers_503_with_retry_after(monkeypatch) -> None:
    monkeypatch.setattr(web_api, "MAX_PENDING_JOBS", 2)
    reservations.update({"busy-1": 10, "busy-2": 10})
    try:
        response = TestClient(app).post("/api/compress/stream", params={"filename": "a.txt"}, content=b"hello")
    finally:
        reservations.pop("busy-1")
        reservations.pop("busy-2")
    assert response.status_code == 503
    assert response.headers["retry-after"] == str(web_api.BUSY_RETRY_AFTER_SECONDS)


def test_full_transfer_count_is_refused_before_upload(monkeypatch) -> None:
    transfers.clear()
    monkeypatch.setattr(web_api, "MAX_TRANSFERS", 1)
    reservations["pending"] = 10
    try:
        response = TestClient(app).post("/api/compress/stream", params={"filename": "a.txt"}, content=b"hello")
    finally:
        reservations.pop("pending")
    assert response.status_code == 507


def test_disk_space_promised_to_other_uploads_is_not_handed_out_twice(monkeypatch) -> None:
    monkeypatch.setattr(web_api, "_free_bytes", lambda: 10_000)
    monkeypatch.setattr(web_api, "MIN_FREE_BYTES", 1_000)
    first = web_api._reserve(6_000)
    try:
        with pytest.raises(HTTPException) as refused:
            web_api._reserve(6_000)
        assert refused.value.status_code == 507
    finally:
        web_api._release(first)
    web_api._release(web_api._reserve(6_000))


def test_waiting_for_an_upload_slot_times_out_with_503(monkeypatch) -> None:
    monkeypatch.setattr(web_api, "UPLOAD_SLOT_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(web_api, "upload_slots", asyncio.Semaphore(0))
    response = TestClient(app).post("/api/compress/stream", params={"filename": "a.txt"}, content=b"hello")
    assert response.status_code == 503
    assert not reservations


def test_stalled_upload_is_aborted(monkeypatch) -> None:
    monkeypatch.setattr(web_api, "UPLOAD_IDLE_TIMEOUT_SECONDS", 0.05)

    async def stalls():
        yield b"first chunk"
        await asyncio.sleep(10)
        yield b"never"

    async def consume() -> list[bytes]:
        return [chunk async for chunk in web_api._until_idle(stalls())]

    with pytest.raises(HTTPException) as aborted:
        asyncio.run(consume())
    assert aborted.value.status_code == 408


def test_reads_have_their_own_rate_limit_and_old_clients_are_forgotten(monkeypatch) -> None:
    monkeypatch.setattr(web_api, "RATE_LIMIT_READ_REQUESTS", 5)
    monkeypatch.setattr(web_api, "RATE_LIMIT_MAX_CLIENTS", 3)
    web_api.rate_limit.clear()
    client = TestClient(app)
    statuses = [client.get("/api/health").status_code for _ in range(7)]
    assert statuses[:5] == [200] * 5 and statuses[5:] == [429, 429]
    stale = time.monotonic() - web_api.RATE_LIMIT_WINDOW_SECONDS - 1
    web_api.rate_limit.update({(f"10.0.0.{n}", "read"): (1, stale) for n in range(10)})
    web_api.GuardsMiddleware._over_limit("10.0.1.1", "read", 5)
    assert not any(key[0].startswith("10.0.0.") for key in web_api.rate_limit)
    web_api.rate_limit.clear()


def _request(headers: dict[str, str]) -> Request:
    return Request({"type": "http", "method": "GET", "path": "/", "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]})


def test_frontend_is_served_gzipped_and_cached(tmp_path) -> None:
    (tmp_path / "assets").mkdir()
    script = b"console.log('FileComp');\n" * 2000
    (tmp_path / "assets" / "index-abc123.js").write_bytes(script)
    (tmp_path / "assets" / "font.woff2").write_bytes(b"wOF2" + bytes(range(256)) * 4)
    (tmp_path / "index.html").write_bytes(b"<!doctype html><title>FileComp</title>")
    files = FrontendFiles(tmp_path)

    asset = files.response("assets/index-abc123.js", _request({"Accept-Encoding": "gzip, br"}), immutable=True)
    assert asset.headers["content-encoding"] == "gzip"
    assert gzip.decompress(asset.body) == script
    assert "immutable" in asset.headers["cache-control"]
    assert files.response("assets/index-abc123.js", _request({})).body == script

    font = files.response("assets/font.woff2", _request({"Accept-Encoding": "gzip"}), immutable=True)
    assert "content-encoding" not in font.headers  # woff2 is already compressed
    assert font.media_type == "font/woff2"

    page = files.response("index.html", _request({}))
    assert page.headers["cache-control"] == "no-cache"
    assert files.response("index.html", _request({"If-None-Match": page.headers["etag"]})).status_code == 304
    with pytest.raises(HTTPException):
        files.response("../secret", _request({}))


def test_one_device_cannot_take_every_upload_slot(monkeypatch) -> None:
    monkeypatch.setattr(web_api, "MAX_UPLOADS_PER_CLIENT", 2)
    web_api.client_uploads["testclient"] = 2
    try:
        response = TestClient(app).post("/api/compress/stream", params={"filename": "a.txt"}, content=b"hello")
    finally:
        web_api.client_uploads.clear()
    assert response.status_code == 429
    assert not reservations
    # Finished uploads give the device its slots back.
    transfers.clear()
    response = TestClient(app).post("/api/compress/stream", params={"filename": "a.txt"}, content=b"hello")
    assert response.status_code == 200
    assert not web_api.client_uploads
