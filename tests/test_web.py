from fastapi.testclient import TestClient
import time

import filecompression.web.api as web_api
from filecompression.web.api import STORAGE_DIR, app, transfers


def test_web_health_algorithms_and_network_info() -> None:
    client = TestClient(app)
    health = client.get("/api/health")
    assert health.json()["status"] == "online"
    assert health.json()["active_operations"] == 0
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
    assert (STORAGE_DIR / f"{identifier}.fcmp").exists()
    assert transfer["algorithm"] in {"lzw", "stored"}
    assert client.get(f"/api/transfers/{identifier}").json()["status"] == "ready"
    download = client.get(f"/api/transfers/{identifier}/download")
    assert download.status_code == 200
    assert download.content == b"abcabcabc"
    assert 'filename="sample.txt"' in download.headers["content-disposition"]
    compressed = client.get(f"/api/transfers/{identifier}/download?compressed=true")
    assert compressed.content[:4] == b"FCMP"
    assert compressed.content == (STORAGE_DIR / f"{identifier}.fcmp").read_bytes()
    assert 'filename="sample.txt.fcmp"' in compressed.headers["content-disposition"]
    restored = client.post("/api/decompress", files={"file": ("sample.txt.fcmp", compressed.content)})
    assert restored.status_code == 200
    assert restored.content == b"abcabcabc"
    assert client.post("/api/decompress", files={"file": ("junk.fcmp", b"junk")}).status_code == 422
    verified = client.post(f"/api/transfers/{identifier}/verify")
    assert verified.json()["verified"] is True
    assert verified.json()["size"] == 9


def test_web_metrics_endpoint_reports_counters() -> None:
    response = TestClient(app).get("/metrics")
    assert response.status_code == 200
    assert "filecompression_stored_transfers" in response.text


def test_web_api_key_authentication_can_be_enabled() -> None:
    previous = web_api.API_KEY
    web_api.API_KEY = "test-secret"
    try:
        client = TestClient(app)
        assert client.get("/api/algorithms").status_code == 401
        assert client.get("/api/algorithms", headers={"X-API-Key": "test-secret"}).status_code == 200
    finally:
        web_api.API_KEY = previous


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


def test_web_recommends_best_algorithm_and_auto_compression_uses_it() -> None:
    client = TestClient(app)
    recommendation = client.post("/api/recommend", files={"file": ("sample.bin", b"A" * 5000)}).json()
    assert recommendation["recommended"] in {"rle", "lzw"}
    assert recommendation["beneficial"] is True
    assert {item["algorithm"] for item in recommendation["estimates"]} == {"huffman", "lzw", "rle"}
    transfers.clear()
    response = client.post("/api/transfers", files={"file": ("отчёт.txt", b"hello world " * 500), "algorithm": (None, "auto")})
    assert response.status_code == 200
    transfer = response.json()
    assert transfer["algorithm"] == transfer["recommended_algorithm"]
    assert transfer["compression_time"] >= 0
    download = client.get(f"/api/transfers/{transfer['id']}/download")
    assert download.content == b"hello world " * 500
    assert "filename*=UTF-8''%D0%BE%D1%82%D1%87%D1%91%D1%82.txt" in download.headers["content-disposition"]


def _wait_for(client: TestClient, operation_id: str) -> dict:
    for _ in range(500):
        status = client.get(f"/api/compress/{operation_id}/status").json()
        if status["status"] in {"completed", "error", "cancelled"}:
            return status
        time.sleep(0.02)
    raise AssertionError("Compression operation did not finish")


def test_web_raw_stream_upload_compresses_multi_block_file() -> None:
    client = TestClient(app)
    data = (b"sensor,value\n" + b"42,17.5\n" * 400_000)[:3_000_000]
    response = client.post("/api/compress/stream", params={"filename": "log.csv", "algorithm": "auto"}, content=data, headers={"Content-Type": "application/octet-stream"})
    assert response.status_code == 200
    status = _wait_for(client, response.json()["operationId"])
    assert status["status"] == "completed", status
    transfer = client.get(f"/api/transfers/{status['transferId']}").json()
    assert transfer["original_size"] == len(data)
    assert transfer["compressed_size"] < len(data)
    download = client.get(f"/api/transfers/{status['transferId']}/download")
    assert download.headers["content-length"] == str(len(data))
    assert download.content == data


def test_web_archive_import_then_streamed_restore() -> None:
    client = TestClient(app)
    created = client.post("/api/transfers", files={"file": ("notes.txt", b"lorem ipsum " * 2000), "algorithm": (None, "huffman")}).json()
    archive = client.get(f"/api/transfers/{created['id']}/download?compressed=true").content
    imported = client.post("/api/archives", params={"filename": "notes.txt.fcmp"}, content=archive)
    assert imported.status_code == 200
    assert imported.json()["filename"] == "notes.txt"
    assert client.get(f"/api/transfers/{imported.json()['id']}/download").content == b"lorem ipsum " * 2000
    assert client.post("/api/archives", content=b"definitely not an archive" * 10).status_code == 422


def test_web_rejects_unsafe_stream_filename() -> None:
    client = TestClient(app)
    response = client.post("/api/compress/stream", params={"filename": "../evil.sh"}, content=b"data")
    assert response.status_code == 400


def test_web_network_info_prefers_the_address_the_browser_used(monkeypatch) -> None:
    monkeypatch.setattr(web_api, "_lan_addresses", lambda: ["192.168.1.40"])
    client = TestClient(app, base_url="http://192.168.1.40:8000")
    info = client.get("/api/network-info").json()
    assert info["url"] == "http://192.168.1.40:8000"
    assert "http://192.168.1.40:8000" in info["urls"]
    local = TestClient(app, base_url="http://localhost:8000").get("/api/network-info").json()
    assert local["url"] == "http://192.168.1.40:8000"
    monkeypatch.setenv("FILECOMP_PUBLIC_URL", "http://files.lan")
    assert TestClient(app).get("/api/network-info").json()["url"] == "http://files.lan"


def test_web_progress_polling_is_not_rate_limited(monkeypatch) -> None:
    monkeypatch.setattr(web_api, "RATE_LIMIT_REQUESTS", 3)
    web_api.rate_limit.clear()
    client = TestClient(app)
    for _ in range(10):
        assert client.get("/api/health").status_code == 200
    statuses = [client.post("/api/compress/missing/cancel").status_code for _ in range(5)]
    web_api.rate_limit.clear()
    assert statuses[:3] == [404, 404, 404]
    assert 429 in statuses


def test_web_network_info_leaves_out_the_standard_port(monkeypatch) -> None:
    monkeypatch.setattr(web_api, "_lan_addresses", lambda: ["10.42.0.1"])
    monkeypatch.setattr(web_api, "_mdns_hostname", lambda: "filecomp.local")
    monkeypatch.setenv("FILECOMP_WEB_PORT", "80")
    info = TestClient(app, base_url="http://localhost").get("/api/network-info").json()
    assert info["url"] == "http://10.42.0.1"
    assert info["urls"] == ["http://10.42.0.1", "http://filecomp.local"]
    monkeypatch.setenv("FILECOMP_PUBLIC_HOST", "10.42.0.1")
    assert TestClient(app).get("/api/network-info").json()["url"] == "http://10.42.0.1"
