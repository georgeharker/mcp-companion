"""Which MCP protocol era the combiner speaks to each upstream.

A server's ``protocol`` setting (``ServerConfig.protocol``) is ``"auto"`` by
default: before the server is mounted, :func:`resolve` probes its
``server/discover``. A server that lists the 2026-07-28 revision is taken to
handle its own sessions (explicit session ids on its stateful tools — the
model passes them), so the combiner connects to it on that revision over one
shared connection and does not isolate it. Every other server — and every
server that can't be probed here (stdio, OAuth-protected, unreachable) — gets
the stateful ``initialize`` handshake, with isolation and resume as configured.
``"legacy"`` / ``"2026"`` force the choice: every mcp 2 server answers discover
with 2026 by default, so a server that still keeps per-connection state must be
pinned to ``"legacy"`` (or pin itself, as cribsheet does).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

import httpx2
from fastmcp import Client
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

from mcp_combiner.config import ServerConfig, Transport, _interpolate_dict, _interpolate_str
from mcp_combiner.runtime import CLIENT_CONNECT_MODE

logger = logging.getLogger("mcp-combiner")

Era = Literal["legacy", "2026"]

#: The 2026-era client mode (fastmcp ``Client(mode=...)``): adopt the revision
#: directly, no probe of its own.
MODERN_CLIENT_MODE = "2026-07-28"

_PROBE_TIMEOUT_S = 3.0
_eras: dict[str, Era] = {}


def era_of(name: str) -> Era:
    """The era *name* was resolved to (``"legacy"`` until resolved)."""
    return _eras.get(name, "legacy")


def client_mode(name: str) -> str:
    """The fastmcp ``Client(mode=...)`` for connections to upstream *name*."""
    return MODERN_CLIENT_MODE if era_of(name) == "2026" else CLIENT_CONNECT_MODE


async def check_alive(name: str, client: Client[Any]) -> None:
    """Prove upstream *name* answers; raises when it doesn't. ``ping`` exists
    only on the handshake revisions: a 2026-era server is asked
    ``server/discover`` instead (it answers ``Method not found`` to ping)."""
    if era_of(name) == "2026":
        # send_discover, not discover(): the latter answers from its cache.
        await client.session.send_discover(client.protocol_version or MODERN_CLIENT_MODE)
    else:
        await client.ping()


def reset() -> None:
    _eras.clear()


async def resolve(name: str, srv: ServerConfig) -> Era:
    """Decide (and remember) the era for upstream *name*; call before mounting."""
    era = await _decide(name, srv)
    previous = _eras.get(name)
    _eras[name] = era
    if era != previous:
        logger.info("Upstream '%s': %s protocol (setting %r)", name, era, srv.protocol)
    if era == "2026" and srv.isolate:
        logger.warning(
            "Upstream '%s' speaks the 2026-07-28 revision: it handles its own sessions, "
            "so isolate is ignored (set protocol: legacy to keep per-chat sessions)",
            name,
        )
    if era == "2026":
        # Per-chat handshake sessions restored by a handover (or left from before
        # a protocol change) can never be used again: drop them now rather than
        # holding them until their park TTL.
        from mcp_combiner.isolated import REGISTRY

        stale = await REGISTRY.evict_server(name)
        if stale:
            logger.info("Upstream '%s': dropped %d stale per-chat session(s)", name, stale)
    return era


async def _decide(name: str, srv: ServerConfig) -> Era:
    if srv.protocol == "legacy":
        return "legacy"
    if srv.protocol == "2026":
        return "2026"
    if srv.transport not in (Transport.HTTP, Transport.SSE) or not srv.url:
        return "legacy"  # stdio: probing would mean spawning it
    if srv.auth:
        return "legacy"  # an unauthenticated probe can't tell; set protocol explicitly
    if srv.transport is Transport.SSE:
        return "legacy"  # the SSE transport is handshake-era only
    try:
        versions = await _discover(srv)
    except Exception as exc:  # noqa: BLE001 — unreachable/odd servers use the handshake
        logger.debug("Upstream '%s': server/discover probe failed (%s); handshake", name, exc)
        return "legacy"
    return "2026" if any(v in MODERN_PROTOCOL_VERSIONS for v in versions) else "legacy"


async def _discover(srv: ServerConfig) -> list[str]:
    """The ``supportedVersions`` a streamable-HTTP server lists on ``server/discover``."""
    url = _interpolate_str(srv.url or "")
    version = sorted(MODERN_PROTOCOL_VERSIONS)[-1]
    headers: dict[str, str] = {
        **(_interpolate_dict(srv.headers) if srv.headers else {}),
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "mcp-protocol-version": version,
        "mcp-method": "server/discover",
    }
    body = {
        "jsonrpc": "2.0",
        "id": "combiner-era-probe",
        "method": "server/discover",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": version,
                "io.modelcontextprotocol/clientInfo": {"name": "mcp-combiner", "version": "probe"},
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        },
    }
    async with httpx2.AsyncClient(timeout=_PROBE_TIMEOUT_S) as http:
        response = await http.post(url, headers=headers, json=body)
    return parse_discover(response.status_code, response.text)


def parse_discover(status_code: int, text: str) -> list[str]:
    """The versions a ``server/discover`` HTTP response lists; ``[]`` for an
    error status, a JSON-RPC error, or a body that isn't a discover result."""
    if status_code >= 400:
        return []
    message = _first_json_message(text)
    result = message.get("result") if isinstance(message, dict) else None
    supported = result.get("supportedVersions") if isinstance(result, dict) else None
    return [v for v in supported if isinstance(v, str)] if isinstance(supported, list) else []


def _first_json_message(text: str) -> Any:
    """The JSON-RPC message in a plain-JSON or SSE (``data: ...``) response
    body, or ``None`` when there is none or it isn't JSON."""
    stripped = text.strip()
    if stripped.startswith("{"):
        payload = stripped
    else:
        data = [line for line in stripped.splitlines() if line.startswith("data:")]
        if not data:
            return None
        payload = data[0].removeprefix("data:").strip()
    try:
        return json.loads(payload)
    except ValueError:
        return None
