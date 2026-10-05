"""The advertised UI origin — what widget URLs NAME, as distinct from what the
combiner BINDS (docs/designs/advertised-ui-origin.md).

The remote-client widget leg (un-bien's paired app) cannot reach loopback URLs,
so URL *construction* must name an origin the renderer can route to — a
tailnet address being the designed case. Binding is untouched (`--host`): the
advertise knob changes only the origin prefix of `/ui/<token>/…` URLs.

Modes (MCP_COMBINER_UI_ADVERTISE, default ``auto``):

- ``auto`` — two-tier discovery: (1) ask tailscaled (``tailscale ip -4`` — the
  authoritative answer when the daemon runs), (2) fall back to scanning
  interface addresses for Tailscale's reserved CGNAT range (100.64.0.0/10 —
  with the caveat that some ISPs assign CGNAT space too, which is why tier 1
  exists), (3) neither finds a tailnet → the bind host (loopback today).
- ``loopback`` — force local-only URLs (the pre-feature behavior).
- ``http://host[:port]`` — explicit override, the only machine-specific form,
  for the tailscale-serve (TLS/MagicDNS) variant.

v1 resolves once at boot (tailnet IP changes are rare; a restart heals). The
design doc notes the refinement: a TTL cache behind the URL constructors so
IP changes self-heal without a restart.
"""

from __future__ import annotations

import ipaddress
import shutil
import subprocess

#: Tailscale's reserved address block (Shared Address Space / CGNAT range).
_TAILSCALE_V4_NET = ipaddress.ip_network("100.64.0.0/10")

_ENV = "MCP_COMBINER_UI_ADVERTISE"
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


def resolve_advertised_host(bind_host: str, mode: str | None = None) -> str:
    """The host widget URLs should name. URL construction only — never binding.

    ``mode`` wins over the env (callers resolving explicit config); both
    default to ``$MCP_COMBINER_UI_ADVERTISE`` (``auto``).
    """
    import os

    advertise = (mode or os.environ.get(_ENV) or "auto").strip().lower()
    if advertise in ("", "auto"):
        found = _tailscale_ip() or _interface_scan_ip()
        if found:
            return found
        # Bind-all is a bind, not a name — never construct URLs with it.
        return "127.0.0.1" if bind_host in ("0.0.0.0", "::", "") else bind_host
    if advertise in ("loopback", "local", "none"):
        return bind_host
    # Explicit origin: accept scheme://host[:port] and a bare host alike.
    if "://" in advertise:
        advertise = advertise.split("://", 1)[1]
    return advertise.rstrip("/") or bind_host


def advertised_origin(bind_host: str, port: int, mode: str | None = None) -> str:
    """The full advertised origin for the UI host page (``http://<host>:<port>``)."""
    host = resolve_advertised_host(bind_host, mode)
    return f"http://{host}:{port}"
