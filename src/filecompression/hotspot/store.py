"""SQLite storage for the hotspot journal, device labels, the block list and open sessions."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .parsers import Station

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    kind TEXT NOT NULL,
    mac TEXT NOT NULL,
    ip TEXT,
    hostname TEXT,
    duration INTEGER,
    rx_bytes INTEGER,
    tx_bytes INTEGER
);
CREATE TABLE IF NOT EXISTS labels (mac TEXT PRIMARY KEY, label TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS blocked (mac TEXT PRIMARY KEY, blocked_at REAL NOT NULL);
-- Uploads, compressions and downloads, with the device resolved from its IP at the time.
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    kind TEXT NOT NULL,
    ip TEXT NOT NULL,
    mac TEXT,
    hostname TEXT,
    filename TEXT,
    size INTEGER,
    compressed_size INTEGER,
    sent_bytes INTEGER,
    algorithm TEXT,
    seconds REAL,
    detail TEXT
);
-- Stations connected at the last poll, so a restart neither logs them again nor loses their disconnect.
CREATE TABLE IF NOT EXISTS sessions (
    mac TEXT PRIMARY KEY,
    signal INTEGER,
    connected_seconds INTEGER NOT NULL,
    rx_bytes INTEGER NOT NULL,
    tx_bytes INTEGER NOT NULL
);
"""

CONNECTED = "CONNECTED"
DISCONNECTED = "DISCONNECTED"


class HotspotStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # One connection shared by the poller thread and request threads, serialized by the lock.
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)

    def close(self) -> None:
        with self.lock:
            self.db.close()

    # ------------------------------------------------------------ journal

    def add_event(self, at: float, kind: str, mac: str, ip: str | None, hostname: str | None, *, duration: int | None = None, rx_bytes: int | None = None, tx_bytes: int | None = None) -> int:
        with self.lock:
            cursor = self.db.execute(
                "INSERT INTO events (at, kind, mac, ip, hostname, duration, rx_bytes, tx_bytes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (at, kind, mac, ip, hostname, duration, rx_bytes, tx_bytes),
            )
            return int(cursor.lastrowid)

    def events(self, limit: int, offset: int) -> tuple[list[dict], int]:
        with self.lock:
            rows = self.db.execute(
                "SELECT events.*, labels.label FROM events LEFT JOIN labels USING (mac) ORDER BY events.id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            total = self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return [dict(row) for row in rows], total

    def delete_event(self, event_id: int) -> bool:
        with self.lock:
            return self.db.execute("DELETE FROM events WHERE id = ?", (event_id,)).rowcount > 0

    def clear_events(self) -> int:
        with self.lock:
            return self.db.execute("DELETE FROM events").rowcount

    # ------------------------------------------------------------ activity

    ACTIVITY_FIELDS = ("filename", "size", "compressed_size", "sent_bytes", "algorithm", "seconds", "detail")

    def add_activity(self, at: float, kind: str, ip: str, mac: str | None, hostname: str | None, **fields) -> int:
        values = [fields.get(name) for name in self.ACTIVITY_FIELDS]
        with self.lock:
            cursor = self.db.execute(
                f"INSERT INTO activity (at, kind, ip, mac, hostname, {', '.join(self.ACTIVITY_FIELDS)}) VALUES (?, ?, ?, ?, ?{', ?' * len(values)})",
                (at, kind, ip, mac, hostname, *values),
            )
            return int(cursor.lastrowid)

    def activity(self, limit: int, offset: int) -> tuple[list[dict], int]:
        with self.lock:
            rows = self.db.execute(
                "SELECT activity.*, labels.label FROM activity LEFT JOIN labels USING (mac) ORDER BY activity.id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            total = self.db.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
        return [dict(row) for row in rows], total

    def delete_activity(self, activity_id: int) -> bool:
        with self.lock:
            return self.db.execute("DELETE FROM activity WHERE id = ?", (activity_id,)).rowcount > 0

    def clear_activity(self) -> int:
        with self.lock:
            return self.db.execute("DELETE FROM activity").rowcount

    # ------------------------------------------------------------ labels

    def labels(self) -> dict[str, str]:
        with self.lock:
            return {row["mac"]: row["label"] for row in self.db.execute("SELECT mac, label FROM labels")}

    def set_label(self, mac: str, label: str | None) -> None:
        with self.lock:
            if label:
                self.db.execute("INSERT INTO labels (mac, label) VALUES (?, ?) ON CONFLICT(mac) DO UPDATE SET label = excluded.label", (mac, label))
            else:
                self.db.execute("DELETE FROM labels WHERE mac = ?", (mac,))

    # ------------------------------------------------------------ block list

    def blocked(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT blocked.mac, blocked.blocked_at, labels.label FROM blocked LEFT JOIN labels USING (mac) ORDER BY blocked_at DESC").fetchall()
        return [dict(row) for row in rows]

    def blocked_macs(self) -> set[str]:
        with self.lock:
            return {row["mac"] for row in self.db.execute("SELECT mac FROM blocked")}

    def add_blocked(self, mac: str, at: float) -> None:
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO blocked (mac, blocked_at) VALUES (?, ?)", (mac, at))

    def remove_blocked(self, mac: str) -> bool:
        with self.lock:
            return self.db.execute("DELETE FROM blocked WHERE mac = ?", (mac,)).rowcount > 0

    # ------------------------------------------------------------ open sessions

    def sessions(self) -> dict[str, Station]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM sessions").fetchall()
        return {row["mac"]: Station(row["mac"], row["signal"], row["connected_seconds"], row["rx_bytes"], row["tx_bytes"]) for row in rows}

    def save_sessions(self, stations: dict[str, Station]) -> None:
        with self.lock:
            self.db.execute("BEGIN")
            try:
                self.db.execute("DELETE FROM sessions")
                self.db.executemany(
                    "INSERT INTO sessions (mac, signal, connected_seconds, rx_bytes, tx_bytes) VALUES (?, ?, ?, ?, ?)",
                    [(s.mac, s.signal, s.connected_seconds, s.rx_bytes, s.tx_bytes) for s in stations.values()],
                )
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
