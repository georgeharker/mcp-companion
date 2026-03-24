"""Persistent HTTP/SSE connection manager for upstream MCP servers.

Keeps ``fastmcp.Client`` sessions alive for the lifetime of the bridge so
that proxy tool-calls reuse the existing TCP+TLS+MCP handshake rather than
paying that cost on every invocation.

Design
------
* Each HTTP/SSE upstream gets a **connected** ``Client`` held open via
  ``AsyncExitStack``.
* A thin factory closure captures a mutable ``[client]`` reference.  The
  factory is passed to ``create_proxy()`` once at mount-time; reconnection
  simply swaps the inner reference — no unmount/remount needed.
* Background health-checks detect dead sessions early and trigger
  reconnection with exponential back-off.
* Stdio servers are unaffected — they use subprocess pipes which are
  already persistent.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastmcp import Client

from mcp_bridge.auth import build_auth
from mcp_bridge.config import (
    BridgeConfig,
    ServerConfig,
    Transport,
    _interpolate_dict,
    _interpolate_str,
)

logger = logging.getLogger("mcp-bridge")

# ---------------------------------------------------------------------------
# Reconnection tuning
# ---------------------------------------------------------------------------
_INITIAL_BACKOFF = 2.0  # seconds
_MAX_BACKOFF = 60.0
_BACKOFF_MULTIPLIER = 2.0
_HEALTH_CHECK_INTERVAL = 30.0  # seconds between keepalive pings


@dataclass
class _ManagedConnection:
    """Internal bookkeeping for one persistent upstream."""

    name: str
    config: BridgeConfig
    srv: ServerConfig
    # Mutable client reference — the factory closure reads client_ref[0]
    client_ref: list[Client | None] = field(default_factory=lambda: [None])
    # Exit stack that owns the ``async with client:`` context
    stack: AsyncExitStack = field(default_factory=AsyncExitStack)
    # Background reconnection / health-check task
    _monitor_task: asyncio.Task[None] | None = field(default=None, repr=False)
    # Current back-off delay (reset on successful connect)
    _backoff: float = field(default=_INITIAL_BACKOFF, repr=False)


class ConnectionManager:
    """Manage persistent ``Client`` connections to HTTP/SSE upstreams.

    Typical lifecycle::

        mgr = ConnectionManager()
        mgr.start_all(config)            # non-blocking — kicks off background tasks
        # ... bridge runs ...
        await mgr.close_all()            # called in lifespan finally

    The optional *on_connected* callback is invoked (from a background task)
    whenever a persistent connection transitions from down → up.  The bridge
    uses this to invalidate the tool cache so the next ``tools/list`` picks
    up the newly-connected server's tools.
    """

    def __init__(self, on_connected: Any | None = None) -> None:
        self._connections: dict[str, _ManagedConnection] = {}
        self._on_connected = on_connected
        self._background_tasks: list[asyncio.Task[None]] = []

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    @staticmethod
    def is_http_server(srv: ServerConfig) -> bool:
        """Return True if *srv* uses an HTTP-based transport."""
        return srv.transport in (Transport.HTTP, Transport.SSE)

    def has_connection(self, name: str) -> bool:
        return name in self._connections

    def get_client_factory(self, name: str) -> Any:
        """Return a zero-arg callable that yields the current connected Client.

        If the connection is down (reconnecting), a *disconnected* client is
        returned so the call falls through to per-call connect/disconnect
        (graceful degradation instead of hard failure).
        """
        conn = self._connections[name]

        def _factory() -> Client:
            client = conn.client_ref[0]
            if client is not None and client.is_connected():
                return client
            # Fallback: return a disconnected copy so ProxyTool.run() does
            # its own connect/disconnect for this one call.
            logger.debug(
                "Persistent connection for '%s' is down — falling back to per-call connect",
                name,
            )
            return _make_disconnected_client(conn.config, name, conn.srv)

        return _factory

    # ------------------------------------------------------------------
    # Connect / disconnect
    # ------------------------------------------------------------------

    def register(self, config: BridgeConfig, name: str, srv: ServerConfig) -> None:
        """Pre-register an HTTP/SSE server without opening a connection.

        This creates the internal bookkeeping entry so that
        ``has_connection()`` returns True and ``get_client_factory()`` can
        be called.  The factory will fall back to per-call connect until
        ``connect()`` or ``connect_all()`` actually opens the session.
        """
        if name in self._connections:
            return
        self._connections[name] = _ManagedConnection(name=name, config=config, srv=srv)

    async def connect(self, config: BridgeConfig, name: str, srv: ServerConfig) -> None:
        """Open a persistent connection to one HTTP/SSE upstream.

        If the server was already registered via ``register()``, this opens
        the connection on the existing entry.  Otherwise it creates the entry
        first.
        """
        conn = self._connections.get(name)
        if conn is None:
            conn = _ManagedConnection(name=name, config=config, srv=srv)
            self._connections[name] = conn
        elif conn.client_ref[0] is not None and conn.client_ref[0].is_connected():
            logger.debug("Connection for '%s' is already open — skipping", name)
            return

        await self._open(conn)

        # Start the background health/reconnect monitor
        if conn._monitor_task is None or conn._monitor_task.done():
            conn._monitor_task = asyncio.create_task(
                self._monitor(conn), name=f"conn-monitor-{name}"
            )

    async def connect_all(self, config: BridgeConfig) -> None:
        """Open persistent connections for every registered HTTP/SSE server.

        Connections are opened concurrently but this method does **not**
        block on them.  Each server connects in a background task so the
        bridge lifespan can ``yield`` immediately and start serving
        requests.  The factory graceful-degradation handles any early
        requests before the persistent connection is ready.
        """
        for name, conn in self._connections.items():
            task = asyncio.create_task(
                self._connect_one(config, name, conn.srv),
                name=f"conn-open-{name}",
            )
            self._background_tasks.append(task)
        if self._background_tasks:
            logger.info(
                "Opening persistent connections in background for %d HTTP server(s): %s",
                len(self._background_tasks),
                [n for n in self._connections],
            )

    async def _connect_one(self, config: BridgeConfig, name: str, srv: ServerConfig) -> None:
        """Background wrapper around ``connect`` — logs but never raises."""
        try:
            await self.connect(config, name, srv)
        except Exception as e:
            logger.warning("Background connect for '%s' failed: %s", name, e)

    async def disconnect(self, name: str) -> None:
        """Tear down the persistent connection for *name*."""
        conn = self._connections.pop(name, None)
        if conn is None:
            return
        await self._teardown(conn)

    async def close_all(self) -> None:
        """Shut down every managed connection (called in lifespan finally)."""
        # Cancel any in-flight background connection tasks
        for task in self._background_tasks:
            if not task.done():
                task.cancel()
        for task in self._background_tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._background_tasks.clear()

        names = list(self._connections)
        for name in names:
            await self.disconnect(name)
        logger.info("All persistent connections closed")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _open(self, conn: _ManagedConnection) -> None:
        """Open the ``async with client:`` context and store the live client."""
        try:
            client = _make_disconnected_client(conn.config, conn.name, conn.srv)
            # Enter the async-with context — this starts the session runner
            await conn.stack.enter_async_context(client)
            conn.client_ref[0] = client
            conn._backoff = _INITIAL_BACKOFF
            logger.info("Persistent connection opened: %s", conn.name)
            # Notify the bridge so it can invalidate the tool cache
            if self._on_connected:
                try:
                    self._on_connected(conn.name)
                except Exception:
                    pass
        except Exception as e:
            logger.warning("Failed to open persistent connection for '%s': %s", conn.name, e)
            conn.client_ref[0] = None

    async def _teardown(self, conn: _ManagedConnection) -> None:
        """Cancel the monitor and close the exit stack."""
        if conn._monitor_task and not conn._monitor_task.done():
            conn._monitor_task.cancel()
            try:
                await conn._monitor_task
            except asyncio.CancelledError:
                pass
        try:
            await conn.stack.aclose()
        except Exception as e:
            logger.debug("Error closing stack for '%s': %s", conn.name, e)
        conn.client_ref[0] = None

    async def _reconnect(self, conn: _ManagedConnection) -> None:
        """Close the old session and open a fresh one."""
        logger.info("Reconnecting to '%s' (backoff=%.1fs) …", conn.name, conn._backoff)

        # Close the old stack — this ends the previous ``async with client:``
        try:
            await conn.stack.aclose()
        except Exception:
            pass
        conn.client_ref[0] = None
        conn.stack = AsyncExitStack()

        await asyncio.sleep(conn._backoff)
        await self._open(conn)

        if conn.client_ref[0] is None:
            # Failed — increase back-off for next attempt
            conn._backoff = min(conn._backoff * _BACKOFF_MULTIPLIER, _MAX_BACKOFF)

    async def _monitor(self, conn: _ManagedConnection) -> None:
        """Background task: periodically verify the session is alive."""
        try:
            while True:
                await asyncio.sleep(_HEALTH_CHECK_INTERVAL)

                client = conn.client_ref[0]
                if client is None or not client.is_connected():
                    logger.warning("Connection to '%s' is down — reconnecting", conn.name)
                    await self._reconnect(conn)
                    continue

                # Lightweight health-check: MCP ping
                try:
                    await asyncio.wait_for(client.ping(), timeout=10.0)
                except Exception as e:
                    logger.warning("Health-check failed for '%s': %s — reconnecting", conn.name, e)
                    await self._reconnect(conn)
        except asyncio.CancelledError:
            return


# ---------------------------------------------------------------------------
# Module-level helper
# ---------------------------------------------------------------------------


def _make_disconnected_client(config: BridgeConfig, name: str, srv: ServerConfig) -> Client:
    """Create a disconnected ``Client`` for the given HTTP/SSE server.

    Ensures that both ``auth`` (httpx.Auth) and static ``headers`` from the
    server config are applied.  The ``headers`` field is how servers like
    GitHub Copilot MCP receive their Bearer token when ``auth`` is not set.
    """
    from fastmcp.client.transports.http import StreamableHttpTransport
    from fastmcp.client.transports.sse import SSETransport

    auth: httpx.Auth | None = build_auth(
        name,
        auth_config=srv.auth,
        server_url=srv.url,
        token_dir=config.oauth.token_dir_path,
        cache_tokens=config.oauth.cache_tokens,
    )

    url = _interpolate_str(srv.url) if srv.url else ""

    # Resolve env-var references in headers (e.g. ${GITHUB_TOKEN})
    headers = _interpolate_dict(srv.headers) if srv.headers else {}

    if headers:
        # Create transport explicitly so we can pass headers.
        # Client.__init__ doesn't accept headers — they must go on the
        # transport.  We pass auth to Client so it calls _set_auth()
        # on the transport for us (avoids double-setting).
        if srv.transport == Transport.SSE:
            transport: StreamableHttpTransport | SSETransport = SSETransport(
                url=url,
                headers=headers,
            )
        else:
            transport = StreamableHttpTransport(url=url, headers=headers)
        return Client(transport, auth=auth)

    # No custom headers — plain URL + optional auth
    return Client(url, auth=auth) if auth else Client(url)
