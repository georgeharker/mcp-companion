"""Which addresses the combiner listens on, and the sockets for them.

The bind setting (CLI ``--host`` > ``MCP_COMBINER_HOST`` > servers.json
``bind.host`` > ``127.0.0.1``) is a list: ``"bind": {"host": ["127.0.0.1",
"auto"]}``, or comma-separated on the CLI / in the env var. Entries:

- an address (``127.0.0.1``, ``::1``, ``100.101.102.103``);
- ``auto`` — this machine's tailnet IPv4, looked up at startup (the tailnet
  detection the widget URLs use, ``ui_host.advertise``; an IP, so no DNS);
- a wildcard (``0.0.0.0`` / ``::``) — every interface; it already covers the
  specific addresses, so a list containing one keeps only the wildcards;
- a host name — resolved when binding; one that doesn't resolve is skipped
  like an address that isn't up. (Prefer ``auto`` to a MagicDNS name: same
  address, no DNS dependency.)

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

from mcp_combiner.tailnet import AUTO

logger = logging.getLogger("mcp-combiner")

DEFAULT_HOST = "127.0.0.1"
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
    """Concrete addresses for *entries*: ``auto`` looked up, wildcards
    collapsed, unavailable aliases dropped (warned). ``[]`` → the default."""
    hosts: list[str] = []
    for entry in entries or [DEFAULT_HOST]:
        if entry.lower() == AUTO:
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
    from mcp_combiner.tailnet import tailnet_ip

    return tailnet_ip()


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
        # gaierror: a name that doesn't resolve (yet) — e.g. a MagicDNS name
        # while Tailscale is down.
        if isinstance(exc, socket.gaierror) or exc.errno in (
            errno.EADDRNOTAVAIL,
            errno.EAFNOSUPPORT,
        ):
            logger.warning("bind: %s:%d isn't available here (%s) — skipped", host, port, exc)
            return None
        raise
    return sock


def unreachable_origins(
    origins: Sequence[tuple[str, str]], hosts: Sequence[str], port: int
) -> list[str]:
    """Warnings for *origins* (``(what, url)``) that name an address this
    combiner isn't listening on, so a browser sent there finds nothing.

    Only plain ``http`` URLs on the combiner's own *port* are checked: anything
    else (``https``, another port) is taken to go through a proxy such as
    ``tailscale serve``, which this process can't see.
    """
    from urllib.parse import urlsplit

    if any(h in WILDCARDS for h in hosts):
        return []
    bound = {_canonical(a) for h in hosts for a in _addresses(h)}
    warnings: list[str] = []
    for what, url in origins:
        parts = urlsplit(url)
        if parts.scheme != "http" or parts.hostname is None:
            continue
        if (parts.port or 80) != port:
            continue
        addresses = {_canonical(a) for a in _addresses(parts.hostname)}
        if not addresses:
            warnings.append(f"{what} {url}: {parts.hostname} doesn't resolve here")
        elif not addresses & bound:
            warnings.append(
                f"{what} {url} names {', '.join(sorted(addresses))}, but the combiner "
                f"listens only on {', '.join(hosts)} — add it to bind.host (e.g. 'auto')"
            )
    return warnings


def _addresses(host: str) -> set[str]:
    try:
        return {str(info[4][0]) for info in socket.getaddrinfo(host, None)}
    except OSError:
        return set()


def _canonical(address: str) -> str:
    try:
        return str(ipaddress.ip_address(address.split("%", 1)[0]))
    except ValueError:
        return address
