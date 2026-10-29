"""The only place that runs system commands: `iw`, `wpa_cli` and `nft`.

Commands are argument lists (never a shell), and every MAC and interface name is checked
against a strict pattern before it is put into one.
"""
from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Iterable

from .parsers import MAC_RE

IFACE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")
NFT_TABLE = "filecomp_hotspot"
COMMAND_TIMEOUT_SECONDS = 5

Runner = Callable[..., subprocess.CompletedProcess]


class HotspotError(Exception):
    """A failure the admin panel shows as is, e.g. a missing tool or missing permissions."""


def require_mac(mac: str) -> str:
    if not MAC_RE.fullmatch(mac):
        raise ValueError(f"Invalid MAC address: {mac!r}")
    return mac


def require_iface(iface: str) -> str:
    if not IFACE_RE.fullmatch(iface):
        raise ValueError(f"Invalid interface name: {iface!r}")
    return iface


def nft_script(iface: str, macs: Iterable[str]) -> str:
    """The whole `inet filecomp_hotspot` table, applied atomically with `nft -f -`.

    Declaring the table first makes the following delete succeed even on the first run, so the
    script is idempotent: it always leaves exactly one drop rule and exactly the given set.
    """
    require_iface(iface)
    elements = sorted({require_mac(mac) for mac in macs})
    set_body = "type ether_addr;" + (f" elements = {{ {', '.join(elements)} }};" if elements else "")
    return (
        f"table inet {NFT_TABLE}\n"
        f"delete table inet {NFT_TABLE}\n"
        f"table inet {NFT_TABLE} {{\n"
        f"  set blocked {{ {set_body} }}\n"
        f"  chain input {{\n"
        f"    type filter hook input priority filter; policy accept;\n"
        f'    iifname "{iface}" ether saddr @blocked drop\n'
        f"  }}\n"
        f"}}\n"
    )


def _permission_hint(stderr: str) -> str:
    lowered = stderr.lower()
    if "not permitted" in lowered or "permission denied" in lowered:
        return " (no permission: the container needs cap_add NET_ADMIN, see docker-compose.yml)"
    return ""


class SystemCommands:
    def __init__(self, iface: str, run: Runner | None = None) -> None:
        self.iface = require_iface(iface)
        self.run = run or subprocess.run

    def _run(self, args: list[str], *, input: str | None = None) -> subprocess.CompletedProcess:
        try:
            return self.run(args, input=input, capture_output=True, text=True, timeout=COMMAND_TIMEOUT_SECONDS, check=False)
        except FileNotFoundError as error:
            raise HotspotError(f"`{args[0]}` is not installed on the server") from error
        except PermissionError as error:
            raise HotspotError(f"No permission to run `{args[0]}`") from error
        except subprocess.TimeoutExpired as error:
            raise HotspotError(f"`{args[0]}` did not answer within {COMMAND_TIMEOUT_SECONDS} s") from error

    def _failure(self, args: list[str], result: subprocess.CompletedProcess) -> HotspotError:
        message = (result.stderr or result.stdout or "").strip().splitlines()
        detail = message[-1] if message else f"exit code {result.returncode}"
        return HotspotError(f"`{' '.join(args[:3])}` failed: {detail}{_permission_hint(result.stderr or '')}")

    def station_dump(self) -> str:
        args = ["iw", "dev", self.iface, "station", "dump"]
        result = self._run(args)
        if result.returncode != 0:
            raise self._failure(args, result)
        return result.stdout

    def kick(self, mac: str) -> str:
        """Disconnect a station; returns the tool that did it.

        wpa_cli asks the access point to deauthenticate the device politely. NetworkManager's
        wpa_supplicant often has no control socket, so `iw station del` is the fallback.
        """
        require_mac(mac)
        try:
            result = self._run(["wpa_cli", "-i", self.iface, "deauthenticate", mac])
            # wpa_cli exits 0 even when the request failed; only a literal OK means success.
            if result.returncode == 0 and result.stdout.strip().endswith("OK"):
                return "wpa_cli"
        except HotspotError:
            pass
        args = ["iw", "dev", self.iface, "station", "del", mac]
        result = self._run(args)
        if result.returncode != 0:
            raise self._failure(args, result)
        return "iw"

    def apply_blocklist(self, macs: Iterable[str]) -> None:
        args = ["nft", "-f", "-"]
        result = self._run(args, input=nft_script(self.iface, macs))
        if result.returncode != 0:
            raise self._failure(args, result)
