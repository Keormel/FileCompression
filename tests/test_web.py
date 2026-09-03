from fastapi.testclient import TestClient

from filecompression.web.api import app, transfers


def test_web_health_algorithms_and_network_info() -> None:
    client = TestClient(app)
    assert client.get("/api/health").json() == {"status": "online"}
    assert [item["id"] for item in client.get("/api/algorithms").json()] == ["huffman", "lzw", "rle"]
    assert client.get("/api/network-info").json()["url"].startswith("http://")


def test_web_transfer_create_inspect_download_and_verify() -> None:
    transfers.clear()
    client = TestClient(app)
    response = client.post("/api/transfers", files={"file": ("sample.txt", b"abcabcabc"), "algorithm": (None, "lzw")})
    assert response.status_code == 200
    transfer = response.json()
    identifier = transfer["id"]
    assert transfer["algorithm"] == "lzw"
    assert client.get(f"/api/transfers/{identifier}").json()["status"] == "ready"
    download = client.get(f"/api/transfers/{identifier}/download")
    assert download.status_code == 200
    assert download.content.startswith(b"FCMP")
    verified = client.post(f"/api/transfers/{identifier}/verify")
    assert verified.json()["verified"] is True
    assert verified.json()["size"] == 9


def test_web_rejects_invalid_algorithm_and_expired_transfer() -> None:
    client = TestClient(app)
    response = client.post("/api/transfers", files={"file": ("sample.txt", b"data"), "algorithm": (None, "unknown")})
    assert response.status_code == 400
    assert client.get("/api/transfers/missing").status_code == 404
