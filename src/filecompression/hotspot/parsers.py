"""Pure parsers for `iw station dump` and dnsmasq lease files, plus the connect/disconnect diff."""
from __future__ import annotations

import re
from dataclasses import dataclass

# Lowercase, colon-separated. Every MAC passes through this before it reaches a command line.
MAC_RE = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")
_INT_RE = re.compile(r"-?\d+")


def normalize_mac(value: str) -> str | None:
    """Lowercase a MAC and accept '-' as separator; None for anything that is not exactly a MAC."""
    mac = value.strip().lower().replace("-", ":")
    return mac if MAC_RE.fullmatch(mac) else None


@dataclass(frozen=True)
class Station:
    mac: str
    signal: int | None = None
    connected_seconds: int = 0
    rx_bytes: int = 0
    tx_bytes: int = 0


@dataclass(frozen=True)
class Lease:
    mac: str
    ip: str
    hostname: str | None
    expiry: int


@dataclass(frozen=True)
class StationChanges:
    connected: list[Station]
    disconnected: list[Station]


def _first_int(value: str) -> int | None:
    match = _INT_RE.search(value)
    return int(match.group()) if match else None


def parse_station_dump(text: str) -> dict[str, Station]:
    """Parse `iw dev <iface> station dump` into stations keyed by MAC.

    Each block starts with `Station <mac> (on <iface>)` followed by tab-indented `key: value` lines;
    `signal` looks like `-45 [-47, -49] dBm` and `connected time` like `123 seconds`.
    """
    stations: dict[str, Station] = {}
    mac: str | None = None
    fields: dict[str, str] = {}

    def flush() -> None:
        if mac is None:
            return
        stations[mac] = Station(
            mac=mac,
            signal=_first_int(fields.get("signal", "")),
            connected_seconds=_first_int(fields.get("connected time", "")) or 0,
            rx_bytes=_first_int(fields.get("rx bytes", "")) or 0,
            tx_bytes=_first_int(fields.get("tx bytes", "")) or 0,
        )

    for line in text.splitlines():
        if line.startswith("Station "):
            flush()
            parts = line.split()
            mac = normalize_mac(parts[1]) if len(parts) > 1 else None
            fields = {}
        elif mac is not None and ":" in line:
            key, _, value = line.strip().partition(":")
            fields[key.strip().lower()] = value.strip()
    flush()
    return stations


def parse_leases(text: str) -> dict[str, Lease]:
    """Parse a dnsmasq lease file (`expiry mac ip hostname clientid` per line) keyed by MAC.

    The hostname is `*` when the device sent none. A MAC with several leases keeps the newest one.
    """
    leases: dict[str, Lease] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        mac = normalize_mac(parts[1])
        if mac is None or not parts[0].isdigit():
            continue
        lease = Lease(mac=mac, ip=parts[2], hostname=None if parts[3] == "*" else parts[3], expiry=int(parts[0]))
        previous = leases.get(mac)
        if previous is None or lease.expiry >= previous.expiry:
            leases[mac] = lease
    return leases


def diff_stations(previous: dict[str, Station], current: dict[str, Station]) -> StationChanges:
    """Stations that appeared and disappeared between two dumps.

    A station whose connected time went down reconnected between the two polls, so its old
    session is reported as ended and a new one as started.
    """
    connected: list[Station] = []
    disconnected: list[Station] = []
    for mac, station in current.items():
        before = previous.get(mac)
        if before is None:
            connected.append(station)
        elif station.connected_seconds < before.connected_seconds:
            disconnected.append(before)
            connected.append(station)
    disconnected.extend(station for mac, station in previous.items() if mac not in current)
    return StationChanges(connected, disconnected)
