"""Polls the access point, keeps the connection journal and applies kicks and blocks."""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .parsers import Lease, Station, diff_stations, parse_leases, parse_station_dump
from .store import CONNECTED, DISCONNECTED, HotspotStore
from .system import HotspotError, SystemCommands, require_mac

logger = logging.getLogger("filecompression.hotspot")
# Open sessions are written to SQLite on every change and otherwise only this often,
# so an idle hotspot does not rewrite the SD card every few seconds.
SESSION_FLUSH_POLLS = 10


class HotspotService:
    def __init__(self, store: HotspotStore, commands: SystemCommands, leases_path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.commands = commands
        self.leases_path = leases_path
        self.clock = clock
        self.current: dict[str, Station] = store.sessions()
        # Last known IP and hostname per MAC: a lease can vanish before the disconnect is noticed.
        self.identity: dict[str, Lease] = {}
        self.error: str | None = None
        self.firewall_error: str | None = None
        self.updated_at: float | None = None
        self.polls_since_flush = 0
        self.lock = threading.Lock()

    def _read_leases(self) -> dict[str, Lease]:
        try:
            return parse_leases(self.leases_path.read_text(errors="replace"))
        except FileNotFoundError:
            return {}  # dnsmasq creates the file with the first lease
        except OSError as error:
            logger.warning("cannot read lease file %s: %s", self.leases_path, error)
            return {}

    def _event(self, at: float, kind: str, station: Station) -> None:
        lease = self.identity.get(station.mac)
        ip, hostname = (lease.ip, lease.hostname) if lease else (None, None)
        if kind == DISCONNECTED:
            self.store.add_event(at, kind, station.mac, ip, hostname, duration=station.connected_seconds, rx_bytes=station.rx_bytes, tx_bytes=station.tx_bytes)
        else:
            self.store.add_event(at, kind, station.mac, ip, hostname)
        logger.info("hotspot %s mac=%s ip=%s hostname=%s", kind.lower(), station.mac, ip, hostname)

    def poll(self) -> None:
        """Read the stations once, log who came and went, and remember the result."""
        with self.lock:
            now = self.clock()
            try:
                dump = self.commands.station_dump()
            except HotspotError as error:
                # Keep the previous state: a tool failure must not look like everyone disconnecting.
                if self.error != str(error):
                    logger.warning("hotspot poll failed: %s", error)
                self.error = str(error)
                self.updated_at = now
                return
            self.error = None
            current = parse_station_dump(dump)
            self.identity.update(self._read_leases())
            changes = diff_stations(self.current, current)
            for station in changes.disconnected:
                self._event(now, DISCONNECTED, station)
            for station in changes.connected:
                self._event(now, CONNECTED, station)
            self.current = current
            self.updated_at = now
            self.polls_since_flush += 1
            if changes.connected or changes.disconnected or self.polls_since_flush >= SESSION_FLUSH_POLLS:
                self.store.save_sessions(current)
                self.polls_since_flush = 0

    def clients(self) -> list[dict]:
        with self.lock:
            stations = list(self.current.values())
            now = self.updated_at or self.clock()
        labels = self.store.labels()
        blocked = self.store.blocked_macs()
        result = []
        for station in sorted(stations, key=lambda item: item.connected_seconds):
            lease = self.identity.get(station.mac)
            result.append({
                "mac": station.mac,
                "ip": lease.ip if lease else None,
                "hostname": lease.hostname if lease else None,
                "label": labels.get(station.mac),
                "signal": station.signal,
                "connectedSeconds": station.connected_seconds,
                "connectedAt": now - station.connected_seconds,
                "rxBytes": station.rx_bytes,
                "txBytes": station.tx_bytes,
                "blocked": station.mac in blocked,
            })
        return result

    def kick(self, mac: str) -> str:
        return self.commands.kick(require_mac(mac))

    def block(self, mac: str) -> bool:
        """Block a MAC in nftables, remember it, and disconnect it; returns whether the kick worked."""
        require_mac(mac)
        # nftables first: if it fails nothing is saved, so the stored list never claims a block that is not there.
        self.commands.apply_blocklist(self.store.blocked_macs() | {mac})
        self.store.add_blocked(mac, self.clock())
        self.firewall_error = None
        try:
            self.commands.kick(mac)
        except HotspotError:
            return False  # not connected right now; the drop rule still holds
        return True

    def unblock(self, mac: str) -> bool:
        require_mac(mac)
        blocked = self.store.blocked_macs()
        if mac not in blocked:
            return False
        self.commands.apply_blocklist(blocked - {mac})
        self.firewall_error = None
        return self.store.remove_blocked(mac)

    def restore_firewall(self) -> None:
        """Rebuild the nftables table from the stored block list (it does not survive a reboot)."""
        try:
            self.commands.apply_blocklist(self.store.blocked_macs())
            self.firewall_error = None
        except HotspotError as error:
            self.firewall_error = str(error)
            logger.warning("cannot restore the hotspot block list in nftables: %s", error)

    def close(self) -> None:
        with self.lock:
            self.store.save_sessions(self.current)
        self.store.close()

    async def run(self, interval: float) -> None:
        while True:
            try:
                await asyncio.to_thread(self.poll)
            except Exception:  # never let the poller die; the next round tries again
                logger.exception("hotspot poll crashed")
            await asyncio.sleep(interval)
