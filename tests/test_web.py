from fastapi.testclient import TestClient
import time

from filecompression.web.api import app, transfers


def test_web_health_algorithms_and_network_info() -> None:
    client = TestClient(app)
    health = client.get("/api/health")
    assert health.json() == {"status": "online"}
    assert health.headers["x-content-type-options"] == "nosniff"
    assert health.headers["x-frame-options"] == "DENY"
    assert [item["id"] for item in client.get("/api/algorithms").json()] == ["huffman", "lzw", "rle", "stored"]
    assert client.get("/api/network-info").json()["url"].startswith("http://")


def test_web_transfer_create_inspect_download_and_verify() -> None:
    transfers.clear()
    client = TestClient(app)
    response = client.post("/api/transfers", files={"file": ("sample.txt", b"abcabcabc"), "algorithm": (None, "lzw")})
    assert response.status_code == 200
    transfer = response.json()
    identifier = transfer["id"]
    assert transfer["algorithm"] in {"lzw", "stored"}
    assert client.get(f"/api/transfers/{identifier}").json()["status"] == "ready"
    download = client.get(f"/api/transfers/{identifier}/download")
    assert download.status_code == 200
    assert download.content == b"abcabcabc"
    assert 'filename="sample.txt"' in download.headers["content-disposition"]
    verified = client.post(f"/api/transfers/{identifier}/verify")
    assert verified.json()["verified"] is True
    assert verified.json()["size"] == 9


def test_web_rejects_invalid_algorithm_and_expired_transfer() -> None:
    client = TestClient(app)
    response = client.post("/api/transfers", files={"file": ("sample.txt", b"data"), "algorithm": (None, "unknown")})
    assert response.status_code == 400
    assert client.get("/api/transfers/missing").status_code == 404


def test_web_rejects_unsafe_upload_filename_and_sessions_by_default() -> None:
    client = TestClient(app)
    response = client.post("/api/transfers", files={"file": ("../secret.txt", b"data"), "algorithm": (None, "rle")})
    assert response.status_code == 400
    session = client.post("/api/sessions", data={"host": "127.0.0.1", "port": "8765", "user": "user_a"})
    assert session.status_code == 404


def test_web_compression_operation_reports_completion() -> None:
    client = TestClient(app)
    response = client.post("/api/compress", files={"file": ("sample.txt", b"A" * 10000), "algorithm": (None, "rle")})
    assert response.status_code == 200
    operation = response.json()
    assert operation["status"] in {"queued", "processing"}
    for _ in range(30):
        status = client.get(f"/api/compress/{operation['operationId']}/status").json()
        if status["status"] == "completed":
            assert status["progress"] == 100
            assert status["processedBytes"] == status["totalBytes"]
            assert status["transferId"]
            return
        time.sleep(0.02)
    raise AssertionError("Compression operation did not complete")
