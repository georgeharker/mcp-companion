"""Which addresses the combiner listens on, and the sockets for them.

The bind setting (CLI ``--host`` > ``MCP_COMBINER_HOST`` > servers.json
``bind.host`` > ``127.0.0.1``) is a list: ``"bind": {"host": ["127.0.0.1",
"tailscale"]}``, or comma-separated on the CLI / in the env var. Entries:

- an address (``127.0.0.1``, ``::1``, ``100.101.102.103``);
- ``tailscale`` — this machine's tailnet IPv4, looked up at startup (the same
  detection the widget URLs use, ``ui_host.advertise``);
- a wildcard (``0.0.0.0`` / ``::``) — every interface; it already covers the
  specific addresses, so a list containing one keeps only the wildcards.

An address that isn't up when the combiner starts (Tailscale down, an
interface gone) is skipped with a warning rather than failing startup; a
restart binds it once it's back. Startup fails only when none bind. A port in
use is still fatal.
"""

from __future__ import annotations

import errno
import ipaddress
import logging
import socket
from collections.abc import Sequence

logger = logging.getLogger("mcp-combiner")

DEFAULT_HOST = "127.0.0.1"
TAILSCALE = "tailscale"
WILDCARDS = frozenset({"0.0.0.0", "::"})


def parse_hosts(value: str | Sequence[str] | None) -> list[str]:
    """Bind entries from a comma-separated string or a list, in order, deduplicated."""
    if value is None:
        return []
    items = value.split(",") if isinstance(value, str) else [str(v) for v in value]
    hosts: list[str] = []
    for item in items:
        host = item.strip()
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def resolve_hosts(entries: Sequence[str]) -> list[str]:
    """Concrete addresses for *entries*: ``tailscale`` looked up, wildcards
    collapsed, unavailable aliases dropped (warned). ``[]`` → the default."""
    hosts: list[str] = []
    for entry in entries or [DEFAULT_HOST]:
        if entry.lower() == TAILSCALE:
            address = tailnet_address()
            if address is None:
                logger.warning(
                    "bind: no Tailscale address on this machine (tailscaled down?) — "
                    "not listening on the tailnet; restart once it's up"
                )
                continue
            entry = address
        if entry not in hosts:
            hosts.append(entry)
    wildcards = [h for h in hosts if h in WILDCARDS]
    if wildcards and len(wildcards) < len(hosts):
        logger.info("bind: %s covers every interface; ignoring the other addresses", wildcards)
        return wildcards
    return hosts


def tailnet_address() -> str | None:
    """This machine's Tailscale IPv4, or ``None``."""
    from mcp_combiner.ui_host.advertise import _interface_scan_ip, _tailscale_ip

    return _tailscale_ip() or _interface_scan_ip()


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def bind_sockets(hosts: Sequence[str], port: int) -> list[socket.socket]:
    """Listening sockets on *port* for each of *hosts*. An address that isn't
    available here is skipped (warned); any other failure — a port in use —
    raises. Raises ``OSError`` when nothing bound."""
    sockets: list[socket.socket] = []
    try:
        for host in hosts:
            sock = _bind_one(host, port)
            if sock is not None:
                sockets.append(sock)
    except BaseException:
        for sock in sockets:
            sock.close()
        raise
    if not sockets:
        raise OSError(errno.EADDRNOTAVAIL, f"none of the bind addresses {list(hosts)} is available")
    return sockets


def _bind_one(host: str, port: int) -> socket.socket | None:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            # Keep "::" from also claiming IPv4, so it can sit beside 0.0.0.0.
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        sock.bind((host, port))
        sock.listen(2048)
    except OSError as exc:
        sock.close()
        if exc.errno in (errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT):
            logger.warning("bind: %s:%d isn't available here (%s) — skipped", host, port, exc)
            return None
        raise
    return sock
