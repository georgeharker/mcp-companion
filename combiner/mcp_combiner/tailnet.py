"""This machine on its tailnet: MagicDNS name and IPv4, looked up once.

Stdlib-only on purpose: serve mode starts the lookup (:func:`prefetch_tailnet_info`)
before its heavy imports, so ``tailscale``'s startup (on macOS it is the
Tailscale app binary, ~0.3-0.5 s per call) overlaps them instead of adding to
startup. Used by bind ``auto``, the advertised widget origin, and
``oauth.public_url``.
"""

from __future__ import annotations

import ipaddress
import shutil
import subprocess
import threading

#: Tailscale's reserved address block (Shared Address Space / CGNAT range).
_TAILSCALE_V4_NET = ipaddress.ip_network("100.64.0.0/10")

_TIER1_TIMEOUT_S = 2.0


def _tailscale_ip() -> str | None:
    """Tier 1: ask the tailscale daemon. ``None`` when it isn't there."""
    if shutil.which("tailscale") is None:
        return None
    try:
        out = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=_TIER1_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in (out.stdout or "").splitlines():
        cand = line.strip()
        if not cand or "/" in cand:  # skip CIDR forms and blanks
            continue
        try:
            addr = ipaddress.ip_address(cand)
        except ValueError:
            continue
        if addr in _TAILSCALE_V4_NET:
            return cand
    return None


#: A setting's whole value meaning "this machine's tailnet" (bind, advertise,
#: oauth.public_url), and the same as a host placeholder inside a URL.
AUTO = "auto"
AUTO_HOST = "{auto}"


def tailnet_host() -> str | None:
    """This machine's tailnet name for URLs: MagicDNS name, else tailnet IP."""
    name, ip = tailnet_info()
    return name or ip


def tailnet_ip() -> str | None:
    """This machine's tailnet IPv4 (for binding: no DNS involved)."""
    return tailnet_info()[1]


# ``tailscale`` can be slow to start (on macOS it is the Tailscale app binary,
# ~0.3-0.5 s per call) and bind, advertise and oauth.public_url all ask: look
# up once per process. A tailnet address change needs a restart, as before.
_info_lock = threading.Lock()
_info: tuple[str | None, str | None] | None = None


def tailnet_info() -> tuple[str | None, str | None]:
    """(MagicDNS name, tailnet IPv4) for this machine, looked up once.

    One ``tailscale status --json`` gives both; without it (no CLI, daemon
    down) the separate ``tailscale ip`` / interface-scan tiers fill the IP.
    """
    global _info
    with _info_lock:
        if _info is None:
            name, ip = _tailscale_status()
            _info = (name, ip or _tailscale_ip() or _interface_scan_ip())
        return _info


def prefetch_tailnet_info() -> None:
    """Start the lookup in the background, so it overlaps process startup.
    Nothing to start without a ``tailscale`` CLI: the fallback scan is cheap
    and runs on first use instead."""
    if shutil.which("tailscale") is None:
        return
    threading.Thread(target=tailnet_info, name="tailnet-info", daemon=True).start()


def reset_tailnet_info() -> None:
    global _info
    with _info_lock:
        _info = None


def _tailscale_status() -> tuple[str | None, str | None]:
    """(MagicDNS name, tailnet IPv4) from one ``tailscale status --json``."""
    import json

    if shutil.which("tailscale") is None:
        return None, None
    try:
        out = subprocess.run(
            ["tailscale", "status", "--json", "--peers=false"],
            capture_output=True,
            text=True,
            timeout=_TIER1_TIMEOUT_S,
            check=False,
        )
        status = json.loads(out.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None, None
    me = status.get("Self") if isinstance(status, dict) else None
    if not isinstance(me, dict):
        return None, None
    raw_name = me.get("DNSName")
    name = raw_name.rstrip(".") or None if isinstance(raw_name, str) else None
    ip = None
    for cand in me.get("TailscaleIPs") or []:
        try:
            if isinstance(cand, str) and ipaddress.ip_address(cand) in _TAILSCALE_V4_NET:
                ip = cand
                break
        except ValueError:
            continue
    return name, ip


def _interface_scan_ip() -> str | None:
    """Tier 2: scan interface addresses for the CGNAT range.

    ``ifconfig`` (macOS/Linux both commonly ship it) parsed best-effort — a
    heuristic fallback for tailscaled-absent setups, tolerant of the ISP-CGNAT
    false positive the design doc warns about only because tier 1 outranks it.
    """
    ifc = shutil.which("ifconfig")
    if ifc is None:
        return None
    try:
        out = subprocess.run(
            [ifc, "-a"],
            capture_output=True,
            text=True,
            timeout=_TIER1_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in (out.stdout or "").splitlines():
        line = line.strip()
        # "inet 100.101.102.103 netmask ..." — skip 127.x and IPv6 lines.
        if not line.startswith("inet "):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        cand = parts[1]
        try:
            addr = ipaddress.ip_address(cand)
        except ValueError:
            continue
        if addr in _TAILSCALE_V4_NET:
            return cand
    return None
