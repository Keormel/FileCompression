import base64
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

import filecompression.web.api as web_api
import filecompression.web.hotspot as web_hotspot
from filecompression.web import activity
from filecompression.hotspot import (
    CONNECTED,
    DISCONNECTED,
    HotspotService,
    HotspotStore,
    Lease,
    Station,
    SystemCommands,
    diff_stations,
    nft_script,
    normalize_mac,
    parse_leases,
    parse_station_dump,
)

STATION_DUMP = """\
Station 3a:F1:0c:22:9b:01 (on wlan0)
\tinactive time:\t320 ms
\trx bytes:\t1048576
\trx packets:\t900
\ttx bytes:\t2097152
\ttx packets:\t1200
\ttx retries:\t3
\ttx failed:\t0
\tsignal:  \t-45 [-47, -49] dBm
\tsignal avg:\t-46 [-48, -50] dBm
\ttx bitrate:\t72.2 MBit/s MCS 7 short GI
\tauthorized:\tyes
\tauthenticated:\tyes
\tconnected time:\t125 seconds
\tassociated at [boottime]:\t812.402s
Station 8c:85:90:aa:bb:cc (on wlan0)
\trx bytes:\t500
\ttx bytes:\t700
\tsignal:  \t-71 dBm
\tconnected time:\t4 seconds
"""

LEASES = """\
1761750000 3a:f1:0c:22:9b:01 10.42.0.57 Pixel-7 01:3a:f1:0c:22:9b:01
1761740000 3a:f1:0c:22:9b:01 10.42.0.12 old-name *
1761750100 8c:85:90:aa:bb:cc 10.42.0.88 * *
broken line
1761750200 not-a-mac 10.42.0.99 x *
"""


def test_parse_station_dump_reads_every_station() -> None:
    stations = parse_station_dump(STATION_DUMP)
    assert list(stations) == ["3a:f1:0c:22:9b:01", "8c:85:90:aa:bb:cc"]
    phone = stations["3a:f1:0c:22:9b:01"]
    assert phone == Station("3a:f1:0c:22:9b:01", signal=-45, connected_seconds=125, rx_bytes=1048576, tx_bytes=2097152)
    assert stations["8c:85:90:aa:bb:cc"].signal == -71
    assert parse_station_dump("") == {}


def test_parse_leases_keeps_newest_lease_and_hides_star_hostname() -> None:
    leases = parse_leases(LEASES)
    assert set(leases) == {"3a:f1:0c:22:9b:01", "8c:85:90:aa:bb:cc"}
    assert leases["3a:f1:0c:22:9b:01"].ip == "10.42.0.57"
    assert leases["3a:f1:0c:22:9b:01"].hostname == "Pixel-7"
    assert leases["8c:85:90:aa:bb:cc"].hostname is None


def test_diff_stations_reports_new_gone_and_reconnected() -> None:
    a = Station("aa:aa:aa:aa:aa:aa", connected_seconds=10)
    b = Station("bb:bb:bb:bb:bb:bb", connected_seconds=50)
    c = Station("cc:cc:cc:cc:cc:cc", connected_seconds=1)
    b_again = Station("bb:bb:bb:bb:bb:bb", connected_seconds=2)
    changes = diff_stations({a.mac: a, b.mac: b}, {b.mac: b_again, c.mac: c})
    assert {s.mac for s in changes.connected} == {b.mac, c.mac}
    assert set(changes.disconnected) == {b, a}
    assert diff_stations({a.mac: a}, {a.mac: Station(a.mac, connected_seconds=13)}).connected == []


@pytest.mark.parametrize("value", ["aa:bb:cc:dd:ee", "aa:bb:cc:dd:ee:fg", "aa:bb:cc:dd:ee:ff; reboot", "../../etc", "", "aa:bb:cc:dd:ee:ff:00"])
def test_normalize_mac_rejects_anything_but_a_mac(value: str) -> None:
    assert normalize_mac(value) is None


def test_normalize_mac_accepts_upper_case_and_dashes() -> None:
    assert normalize_mac("AA-BB-CC-DD-EE-0F") == "aa:bb:cc:dd:ee:0f"


def test_nft_script_is_idempotent_table_with_drop_rule() -> None:
    script = nft_script("wlan0", ["bb:bb:bb:bb:bb:bb", "aa:aa:aa:aa:aa:aa"])
    assert script.startswith("table inet filecomp_hotspot\ndelete table inet filecomp_hotspot\n")
    assert "elements = { aa:aa:aa:aa:aa:aa, bb:bb:bb:bb:bb:bb }" in script
    assert 'iifname "wlan0" ether saddr @blocked drop' in script
    assert "elements" not in nft_script("wlan0", [])
    with pytest.raises(ValueError):
        nft_script("wlan0", ["aa:aa:aa:aa:aa:aa }; flush ruleset; {"])
    with pytest.raises(ValueError):
        nft_script('wlan0" accept', [])


class FakeSystem:
    """Stands in for subprocess.run and records every command."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.inputs: list[str | None] = []
        self.dump = ""
        self.wpa_reply = "FAIL"
        self.missing: set[str] = set()
        self.failing: set[str] = set()

    def __call__(self, args, input=None, **kwargs):
        assert isinstance(args, list) and not kwargs.get("shell")
        self.calls.append(args)
        self.inputs.append(input)
        if args[0] in self.missing:
            raise FileNotFoundError(args[0])
        if args[0] in self.failing:
            return subprocess.CompletedProcess(args, 1, "", "Operation not permitted")
        if args[:2] == ["iw", "dev"] and args[3:] == ["station", "dump"]:
            return subprocess.CompletedProcess(args, 0, self.dump, "")
        if args[0] == "wpa_cli":
            return subprocess.CompletedProcess(args, 0, self.wpa_reply + "\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    def commands(self, tool: str) -> list[list[str]]:
        return [call for call in self.calls if call[0] == tool]


@pytest.fixture
def system() -> FakeSystem:
    return FakeSystem()


@pytest.fixture
def hotspot(tmp_path, system):
    leases = tmp_path / "dnsmasq-wlan0.leases"
    leases.write_text(LEASES)
    clock = iter(range(1_000_000, 2_000_000, 3))
    service = HotspotService(HotspotStore(tmp_path / "hotspot.sqlite3"), SystemCommands("wlan0", run=system), leases, clock=lambda: float(next(clock)))
    yield service
    service.store.db.close()


def test_poll_logs_connect_and_disconnect_with_traffic(hotspot, system) -> None:
    system.dump = STATION_DUMP
    hotspot.poll()
    events, total = hotspot.store.events(10, 0)
    assert total == 2 and {e["kind"] for e in events} == {CONNECTED}
    phone = next(e for e in events if e["mac"] == "3a:f1:0c:22:9b:01")
    assert (phone["ip"], phone["hostname"]) == ("10.42.0.57", "Pixel-7")

    hotspot.poll()  # nothing changed
    assert hotspot.store.events(10, 0)[1] == 2

    system.dump = STATION_DUMP.split("Station 8c")[0]
    hotspot.poll()
    events, total = hotspot.store.events(10, 0)
    assert total == 3
    gone = events[0]
    assert gone["kind"] == DISCONNECTED and gone["mac"] == "8c:85:90:aa:bb:cc"
    assert (gone["duration"], gone["rx_bytes"], gone["tx_bytes"]) == (4, 500, 700)


def test_poll_failure_keeps_state_and_reports_error(hotspot, system) -> None:
    system.dump = STATION_DUMP
    hotspot.poll()
    system.missing.add("iw")
    hotspot.poll()
    assert "`iw` is not installed" in hotspot.error
    assert len(hotspot.clients()) == 2  # a broken tool is not "everyone left"
    assert hotspot.store.events(10, 0)[1] == 2


def test_open_sessions_survive_restart(tmp_path, hotspot, system) -> None:
    system.dump = STATION_DUMP
    hotspot.poll()
    hotspot.close()
    reopened = HotspotService(HotspotStore(tmp_path / "hotspot.sqlite3"), SystemCommands("wlan0", run=system), hotspot.leases_path)
    system.dump = ""
    reopened.poll()
    events, total = reopened.store.events(10, 0)
    assert total == 4 and [e["kind"] for e in events[:2]] == [DISCONNECTED, DISCONNECTED]
    reopened.store.close()


def test_kick_falls_back_to_iw_when_wpa_cli_fails(hotspot, system) -> None:
    assert hotspot.kick("3a:f1:0c:22:9b:01") == "iw"
    assert system.commands("wpa_cli") == [["wpa_cli", "-i", "wlan0", "deauthenticate", "3a:f1:0c:22:9b:01"]]
    assert system.commands("iw") == [["iw", "dev", "wlan0", "station", "del", "3a:f1:0c:22:9b:01"]]
    system.calls.clear()
    system.wpa_reply = "OK"
    assert hotspot.kick("3a:f1:0c:22:9b:01") == "wpa_cli"
    assert system.commands("iw") == []


@pytest.fixture
def api(hotspot, monkeypatch):
    monkeypatch.setattr(web_hotspot, "ADMIN_PASSWORD", "s3cret")
    monkeypatch.setattr(web_hotspot, "service", hotspot)
    web_hotspot.auth_failures.clear()
    web_api.rate_limit.clear()
    client = TestClient(web_api.app)
    client.headers["Authorization"] = "Basic " + base64.b64encode(b"admin:s3cret").decode()
    yield client
    web_api.rate_limit.clear()


def test_api_is_hidden_without_admin_password(monkeypatch) -> None:
    monkeypatch.setattr(web_hotspot, "ADMIN_PASSWORD", "")
    client = TestClient(web_api.app)
    assert client.get("/api/hotspot/clients").status_code == 404
    assert client.post("/api/hotspot/blocked/aa:bb:cc:dd:ee:ff").status_code == 404


def test_api_requires_password_and_throttles_guessing(api) -> None:
    anonymous = TestClient(web_api.app)
    assert anonymous.get("/api/hotspot/clients").status_code == 401
    assert "www-authenticate" not in anonymous.get("/api/hotspot/clients").headers
    wrong = {"Authorization": "Basic " + base64.b64encode(b"admin:nope").decode()}
    statuses = [anonymous.get("/api/hotspot/events", headers=wrong).status_code for _ in range(web_hotspot.MAX_AUTH_FAILURES + 1)]
    assert statuses[-1] == 429


def test_api_clients_events_and_labels(api, system) -> None:
    system.dump = STATION_DUMP
    clients = api.get("/api/hotspot/clients").json()
    assert clients["error"] is None
    phone = next(c for c in clients["clients"] if c["mac"] == "3a:f1:0c:22:9b:01")
    assert (phone["ip"], phone["hostname"], phone["signal"], phone["rxBytes"]) == ("10.42.0.57", "Pixel-7", -45, 1048576)

    assert api.put("/api/hotspot/labels/3A-F1-0C-22-9B-01", json={"label": "телефон Саши"}).status_code == 200
    events = api.get("/api/hotspot/events?limit=1").json()
    assert events["total"] == 2 and len(events["events"]) == 1
    labelled = api.get("/api/hotspot/events").json()["events"]
    assert any(e["label"] == "телефон Саши" for e in labelled)

    first = labelled[0]["id"]
    assert api.delete(f"/api/hotspot/events/{first}").status_code == 200
    assert api.delete(f"/api/hotspot/events/{first}").status_code == 404
    assert api.delete("/api/hotspot/events").json() == {"deleted": 1}
    assert api.get("/api/hotspot/events").json()["total"] == 0


def test_api_block_unblock_and_kick(api, system) -> None:
    mac = "8c:85:90:aa:bb:cc"
    blocked = api.post(f"/api/hotspot/blocked/{mac}")
    assert blocked.status_code == 200
    assert blocked.json()["kicked"] is True
    assert system.commands("nft") == [["nft", "-f", "-"]]
    assert f"elements = {{ {mac} }}" in system.inputs[system.calls.index(["nft", "-f", "-"])]
    assert [item["mac"] for item in api.get("/api/hotspot/blocked").json()["blocked"]] == [mac]

    assert api.delete(f"/api/hotspot/blocked/{mac}").status_code == 200
    assert "elements" not in system.inputs[-1]
    assert api.get("/api/hotspot/blocked").json()["blocked"] == []
    assert api.delete(f"/api/hotspot/blocked/{mac}").status_code == 404

    assert api.post(f"/api/hotspot/clients/{mac}/kick").json() == {"mac": mac, "method": "iw"}


def test_api_rejects_bad_mac_before_running_anything(api, system) -> None:
    for path in ("/api/hotspot/clients/aa:bb;reboot/kick", "/api/hotspot/blocked/not-a-mac", "/api/hotspot/clients/aa:bb:cc:dd:ee:ff:00/kick"):
        assert api.post(path).status_code == 400
    assert system.calls == []


def test_api_reports_missing_tools_without_breaking_the_app(api, system) -> None:
    system.missing.update({"iw", "nft", "wpa_cli"})
    clients = api.get("/api/hotspot/clients")
    assert clients.status_code == 200 and "`iw` is not installed" in clients.json()["error"]
    blocked = api.post("/api/hotspot/blocked/aa:bb:cc:dd:ee:ff")
    assert blocked.status_code == 503 and "`nft` is not installed" in blocked.json()["detail"]
    assert api.get("/api/hotspot/blocked").json()["blocked"] == []  # nothing saved when nft failed
    system.missing.clear()
    system.failing.add("nft")
    assert "NET_ADMIN" in api.post("/api/hotspot/blocked/aa:bb:cc:dd:ee:ff").json()["detail"]
    assert api.get("/api/health").status_code == 200


def _wait_for_compression(client: TestClient, operation_id: str) -> dict:
    for _ in range(200):
        status = client.get(f"/api/compress/{operation_id}/status").json()
        if status["status"] in {"completed", "error", "cancelled"}:
            return status
        time.sleep(0.02)
    raise AssertionError("compression did not finish")


def test_activity_feed_records_uploads_compressions_and_downloads(api, hotspot, system) -> None:
    # TestClient requests come from the address "testclient"; pretend it is the phone's lease.
    hotspot.identity["3a:f1:0c:22:9b:01"] = Lease("3a:f1:0c:22:9b:01", "testclient", "Pixel-7", 0)
    api.put("/api/hotspot/labels/3a:f1:0c:22:9b:01", json={"label": "телефон Саши"})
    data = b"hello hotspot " * 2000
    started = api.post("/api/compress/stream?filename=notes.txt&algorithm=lzw", content=data, headers={"Content-Type": "application/octet-stream"})
    assert started.status_code == 200
    transfer_id = _wait_for_compression(api, started.json()["operationId"])["transferId"]
    assert api.get(f"/api/transfers/{transfer_id}/download?compressed=true").status_code == 200
    assert api.get(f"/api/transfers/{transfer_id}/download", headers={"Range": "bytes=10-"}).status_code in {200, 206}
    hotspot.flush()

    feed = api.get("/api/hotspot/activity").json()
    kinds = [item["kind"] for item in feed["activity"]]
    assert kinds == [activity.DOWNLOAD_ARCHIVE, activity.COMPRESSED, activity.UPLOAD]  # newest first; the Range resume is not logged again
    upload = feed["activity"][-1]
    assert (upload["filename"], upload["size"], upload["sentBytes"], upload["algorithm"]) == ("notes.txt", len(data), len(data), "lzw")
    assert (upload["mac"], upload["hostname"], upload["label"]) == ("3a:f1:0c:22:9b:01", "Pixel-7", "телефон Саши")
    compressed = feed["activity"][1]
    assert compressed["size"] == len(data) and 0 < compressed["compressedSize"] < len(data)
    assert feed["live"] == []

    assert api.delete(f"/api/hotspot/activity/{feed['activity'][0]['id']}").status_code == 200
    assert api.delete("/api/hotspot/activity").json() == {"deleted": 2}


def test_activity_feed_records_failed_upload(api, hotspot) -> None:
    response = api.post("/api/compress/stream?filename=bad.txt&encoding=gzip", content=b"not gzip at all", headers={"Content-Type": "application/octet-stream"})
    assert response.status_code == 400
    hotspot.flush()
    failed = api.get("/api/hotspot/activity").json()["activity"][0]
    assert (failed["kind"], failed["filename"], failed["sentBytes"]) == (activity.UPLOAD_FAILED, "bad.txt", 15)
    assert "gzip" in failed["detail"]


def test_activity_shows_uploads_in_progress(api) -> None:
    upload = activity.start_upload("testclient", "movie.mp4", size=10_000, body_size=10_000, gzipped=False)
    upload.received = 2_500
    try:
        live = api.get("/api/hotspot/activity").json()["live"]
        assert [(item["kind"], item["filename"], item["done"], item["total"]) for item in live] == [(activity.UPLOAD, "movie.mp4", 2_500, 10_000)]
    finally:
        activity.finish_upload(upload)


def test_activity_is_not_stored_while_panel_is_disabled(monkeypatch, hotspot) -> None:
    monkeypatch.setattr(web_hotspot, "ADMIN_PASSWORD", "")
    monkeypatch.setattr(web_hotspot, "service", hotspot)
    activity.record("10.42.0.57", activity.UPLOAD, filename="a.txt", size=1)
    hotspot.flush()
    assert hotspot.store.activity(10, 0)[1] == 0
