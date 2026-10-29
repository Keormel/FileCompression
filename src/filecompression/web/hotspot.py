"""Admin API for the Wi-Fi access point under /api/hotspot/.

Disabled (every route answers 404) unless FILECOMP_HOTSPOT_ADMIN_PASSWORD is set. Requests carry
`Authorization: Basic ...` from the admin page. The server never sends a WWW-Authenticate
challenge, so browsers do not cache the credentials and other sites cannot ride on them.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import logging
import os
import secrets
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from filecompression.hotspot import HotspotError, HotspotService, HotspotStore, SystemCommands, normalize_mac
from filecompression.web import activity

ADMIN_PASSWORD = os.getenv("FILECOMP_HOTSPOT_ADMIN_PASSWORD", "")
IFACE = os.getenv("FILECOMP_HOTSPOT_IFACE", "").strip() or "wlan0"
LEASES_PATH = Path(os.getenv("FILECOMP_HOTSPOT_LEASES", "").strip() or f"/var/lib/NetworkManager/dnsmasq-{IFACE}.leases")
POLL_INTERVAL_SECONDS = 3
DATABASE_NAME = "hotspot.sqlite3"
# Wrong passwords per client address before it has to wait; GET requests have a large rate
# limit budget, so guessing needs its own, much tighter one.
MAX_AUTH_FAILURES = 10
AUTH_FAILURE_WINDOW_SECONDS = 300

logger = logging.getLogger("filecompression.hotspot")
storage_dir = Path(os.getenv("FILECOMP_STORAGE_DIR", str(Path.cwd() / "data")))
service: HotspotService | None = None
auth_failures: dict[str, tuple[int, float]] = {}


def enabled() -> bool:
    return bool(ADMIN_PASSWORD)


def get_service() -> HotspotService:
    global service
    if service is None:
        try:
            commands = SystemCommands(IFACE)
        except ValueError as error:
            raise HTTPException(503, "FILECOMP_HOTSPOT_IFACE is not a valid interface name") from error
        service = HotspotService(HotspotStore(storage_dir / DATABASE_NAME), commands, LEASES_PATH)
    return service


def _record_activity(record: dict) -> None:
    if enabled():
        get_service().record_activity(record)


activity.sink = _record_activity


def _password_from(header: str) -> str | None:
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    return decoded.partition(":")[2]


def require_admin(request: Request) -> HotspotService:
    if not enabled():
        raise HTTPException(404, "Not Found")
    client = request.client.host if request.client else "unknown"
    now = time.monotonic()
    failures, since = auth_failures.get(client, (0, now))
    if now - since >= AUTH_FAILURE_WINDOW_SECONDS:
        failures, since = 0, now
    if failures >= MAX_AUTH_FAILURES:
        raise HTTPException(429, "Too many wrong passwords; try again in a few minutes", headers={"Retry-After": str(AUTH_FAILURE_WINDOW_SECONDS)})
    password = _password_from(request.headers.get("authorization", ""))
    if password is None or not secrets.compare_digest(password.encode(), ADMIN_PASSWORD.encode()):
        auth_failures[client] = (failures + 1, since)
        raise HTTPException(401, "Wrong admin password")
    auth_failures.pop(client, None)
    return get_service()


def _mac(value: str) -> str:
    mac = normalize_mac(value)
    if mac is None:
        raise HTTPException(400, "Invalid MAC address")
    return mac


async def _system(call, *args):
    """Run a blocking call that may touch iw/nft; their failures become a readable 503."""
    try:
        return await asyncio.to_thread(call, *args)
    except HotspotError as error:
        raise HTTPException(503, str(error)) from error


def _event_json(row: dict) -> dict:
    return {
        "id": row["id"],
        "at": row["at"],
        "kind": row["kind"],
        "mac": row["mac"],
        "ip": row["ip"],
        "hostname": row["hostname"],
        "label": row["label"],
        "duration": row["duration"],
        "rxBytes": row["rx_bytes"],
        "txBytes": row["tx_bytes"],
    }


def _activity_json(row: dict) -> dict:
    return {
        "id": row["id"],
        "at": row["at"],
        "kind": row["kind"],
        "ip": row["ip"],
        "mac": row["mac"],
        "hostname": row["hostname"],
        "label": row["label"],
        "filename": row["filename"],
        "size": row["size"],
        "compressedSize": row["compressed_size"],
        "sentBytes": row["sent_bytes"],
        "algorithm": row["algorithm"],
        "seconds": row["seconds"],
        "detail": row["detail"],
    }


class LabelBody(BaseModel):
    label: str = Field("", max_length=64)


router = APIRouter(prefix="/api/hotspot", include_in_schema=False)


@router.get("/clients")
async def list_clients(hotspot: HotspotService = Depends(require_admin)) -> dict:
    # Normally the background poller keeps this fresh; poll here when it is not running.
    if hotspot.updated_at is None or time.time() - hotspot.updated_at > POLL_INTERVAL_SECONDS * 2:
        await asyncio.to_thread(hotspot.poll)
    return {
        "interface": IFACE,
        "clients": await asyncio.to_thread(hotspot.clients),
        "error": hotspot.error,
        "updatedAt": hotspot.updated_at,
    }


@router.post("/clients/{mac}/kick")
async def kick_client(mac: str, hotspot: HotspotService = Depends(require_admin)) -> dict:
    mac = _mac(mac)
    method = await _system(hotspot.kick, mac)
    return {"mac": mac, "method": method}


@router.get("/events")
async def list_events(limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0), hotspot: HotspotService = Depends(require_admin)) -> dict:
    rows, total = await asyncio.to_thread(hotspot.store.events, limit, offset)
    return {"events": [_event_json(row) for row in rows], "total": total}


@router.delete("/events/{event_id}")
async def delete_event(event_id: int, hotspot: HotspotService = Depends(require_admin)) -> dict:
    if not await asyncio.to_thread(hotspot.store.delete_event, event_id):
        raise HTTPException(404, "Event not found")
    return {"deleted": 1}


@router.delete("/events")
async def clear_events(hotspot: HotspotService = Depends(require_admin)) -> dict:
    return {"deleted": await asyncio.to_thread(hotspot.store.clear_events)}


@router.get("/activity")
async def list_activity(limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0), hotspot: HotspotService = Depends(require_admin)) -> dict:
    rows, total = await asyncio.to_thread(hotspot.store.activity, limit, offset)
    labels = await asyncio.to_thread(hotspot.store.labels)
    live = []
    for item in activity.live():
        device = hotspot.who(item["client"])
        live.append({**item, **device, "label": labels.get(device["mac"]) if device["mac"] else None})
    return {"live": live, "activity": [_activity_json(row) for row in rows], "total": total}


@router.delete("/activity/{activity_id}")
async def delete_activity(activity_id: int, hotspot: HotspotService = Depends(require_admin)) -> dict:
    if not await asyncio.to_thread(hotspot.store.delete_activity, activity_id):
        raise HTTPException(404, "Activity record not found")
    return {"deleted": 1}


@router.delete("/activity")
async def clear_activity(hotspot: HotspotService = Depends(require_admin)) -> dict:
    return {"deleted": await asyncio.to_thread(hotspot.store.clear_activity)}


@router.get("/blocked")
async def list_blocked(hotspot: HotspotService = Depends(require_admin)) -> dict:
    rows = await asyncio.to_thread(hotspot.store.blocked)
    return {"blocked": [{"mac": row["mac"], "blockedAt": row["blocked_at"], "label": row["label"]} for row in rows], "error": hotspot.firewall_error}


@router.post("/blocked/{mac}")
async def block_client(mac: str, hotspot: HotspotService = Depends(require_admin)) -> dict:
    mac = _mac(mac)
    kicked = await _system(hotspot.block, mac)
    return {"mac": mac, "blocked": True, "kicked": kicked}


@router.delete("/blocked/{mac}")
async def unblock_client(mac: str, hotspot: HotspotService = Depends(require_admin)) -> dict:
    mac = _mac(mac)
    if not await _system(hotspot.unblock, mac):
        raise HTTPException(404, "MAC is not blocked")
    return {"mac": mac, "blocked": False}


@router.put("/labels/{mac}")
async def set_label(mac: str, body: LabelBody, hotspot: HotspotService = Depends(require_admin)) -> dict:
    mac = _mac(mac)
    label = body.label.strip() or None
    await asyncio.to_thread(hotspot.store.set_label, mac, label)
    return {"mac": mac, "label": label}


async def start() -> asyncio.Task | None:
    """Restore the block list in nftables and start polling; nothing happens while the panel is disabled."""
    if not enabled():
        return None
    try:
        hotspot = get_service()
    except HTTPException as error:
        logger.warning("hotspot panel not started: %s", error.detail)
        return None
    await asyncio.to_thread(hotspot.restore_firewall)
    return asyncio.create_task(hotspot.run(POLL_INTERVAL_SECONDS))


async def stop(task: asyncio.Task | None) -> None:
    global service
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    if service is not None:
        await asyncio.to_thread(service.close)
        service = None

