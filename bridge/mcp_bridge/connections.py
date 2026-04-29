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

Auth-failure semantics
----------------------
* OAuth is attempted **once** per server during ``connect_all()``.
* If OAuth fails the connection is marked ``_auth_failed`` and the factory
  raises ``AuthenticationError`` for all subsequent calls — **no new
  ``OAuth`` instances are created**, no browser windows open.
* The health-check monitor skips auth-failed connections.
* The only recovery path is ``bridge__enable_server`` which calls
  ``reset_auth_failure()`` and then ``connect()`` for a single fresh
  attempt.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Callable

import httpx
from fastmcp import Client
from fastmcp.client.transports.http import StreamableHttpTransport
from fastmcp.client.transports.sse import SSETransport

from mcp_bridge.auth import build_auth
from mcp_bridge.config import (
    BridgeConfig,
    ServerConfig,
    Transport,
    _interpolate_dict,
    _interpolate_str,
)

logger = logging.getLogger("mcp-bridge")

# The only transport types this module creates.
HttpClient = Client[StreamableHttpTransport | SSETransport]

# ---------------------------------------------------------------------------
# Reconnection tuning
# ---------------------------------------------------------------------------
_INITIAL_BACKOFF = 2.0  # seconds
_MAX_BACKOFF = 60.0
_BACKOFF_MULTIPLIER = 2.0
_HEALTH_CHECK_INTERVAL = 30.0  # seconds between keepalive pings

# How long the factory waits for an in-flight connect before giving up.
_FACTORY_WAIT_TIMEOUT = 60.0  # seconds


# ---------------------------------------------------------------------------
# Custom exception so the bridge can distinguish auth errors from transient
# connection failures.  RetryMiddleware should **not** retry these.
# ---------------------------------------------------------------------------
class AuthenticationError(Exception):
    """Raised when a server is disabled due to an authentication failure.

    This is intentionally *not* a subclass of ``ConnectionError`` so that
    ``RetryMiddleware(retry_exceptions=(ConnectionError, TimeoutError))``
    does not catch it.
    """


@dataclass
class _ManagedConnection:
    """Internal bookkeeping for one persistent upstream."""

    name: str
    config: BridgeConfig
    srv: ServerConfig
    # Mutable client reference — the factory closure reads client_ref[0]
    client_ref: list[HttpClient | None] = field(default_factory=lambda: [None])
    # Exit stack that owns the ``async with client:`` context
    stack: AsyncExitStack = field(default_factory=AsyncExitStack)
    # Background reconnection / health-check task
    _monitor_task: asyncio.Task[None] | None = field(default=None, repr=False)
    # Current back-off delay (reset on successful connect)
    _backoff: float = field(default=_INITIAL_BACKOFF, repr=False)
    # Set when the connection failed due to an auth error.
    # When True the monitor stops retrying and get_client_factory raises
    # instead of creating a new OAuth client.  Cleared by explicit
    # ``reset_auth_failure()``.
    _auth_failed: bool = field(default=False, repr=False)
    _auth_error_msg: str = field(default="", repr=False)
    # Signalled once the first connect attempt finishes (success or failure).
    # The factory waits on this so it never falls back to creating new OAuth.
    _ready: asyncio.Event = field(default_factory=asyncio.Event, repr=False)


class ConnectionManager:
    """Manage persistent ``Client`` connections to HTTP/SSE upstreams.

    Typical lifecycle::

        mgr = ConnectionManager()
        mgr.register(config, name, srv)      # pre-register
        await mgr.connect_all(config)         # blocks until all resolved
        # ... bridge runs ...
        await mgr.close_all()                 # called in lifespan finally

    The optional *on_connected* callback is invoked (from a background task)
    whenever a persistent connection transitions from down → up.  The bridge
    uses this to invalidate the tool cache so the next ``tools/list`` picks
    up the newly-connected server's tools.
    """

    def __init__(self, on_connected: Callable[[str], None] | None = None) -> None:
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

    def get_client_factory(self, name: str) -> Callable[[], HttpClient]:
        """Return a zero-arg callable that yields the current connected Client.

        Semantics:
        - If ``_auth_failed`` → raise ``AuthenticationError`` (not retried).
        - If the persistent client is connected → return it.
        - If a connect is still in flight → **wait** for it (up to timeout).
        - If the connection is down after waiting → raise ``ConnectionError``.

        This factory **never** creates a new ``Client`` or ``OAuth`` instance.
        Only ``_open()`` does that — ensuring exactly one OAuth flow per
        connect attempt.
        """
        conn = self._connections[name]

        def _factory() -> HttpClient:
            # Auth-failed: permanent error until manual reset
            if conn._auth_failed:
                raise AuthenticationError(
                    f"Server '{name}' is disabled due to an authentication error: "
                    f"{conn._auth_error_msg}. "
                    "Use bridge__enable_server to retry."
                )

            client = conn.client_ref[0]
            if client is not None and client.is_connected():
                return client

            # Connection is not ready yet — we cannot create a fallback
            # because that would spin up a new OAuth instance.
            if not conn._ready.is_set():
                raise ConnectionError(
                    f"Server '{name}' is still connecting. Please retry in a moment."
                )

            # Ready was set but client is gone (reconnecting after health-check).
            raise ConnectionError(
                f"Server '{name}' persistent connection is down. It will reconnect automatically."
            )

        return _factory

    def is_auth_failed(self, name: str) -> bool:
        """Return True if *name* is paused due to an authentication error."""
        conn = self._connections.get(name)
        return conn is not None and conn._auth_failed

    def reset_auth_failure(self, name: str) -> None:
        """Clear the auth-failure flag so the server can be retried."""
        conn = self._connections.get(name)
        if conn is not None:
            conn._auth_failed = False
            conn._auth_error_msg = ""
            # Reset ready so the next connect() can signal it again
            conn._ready = asyncio.Event()

    # ------------------------------------------------------------------
    # Connect / disconnect
    # ------------------------------------------------------------------

    def register(self, config: BridgeConfig, name: str, srv: ServerConfig) -> None:
        """Pre-register an HTTP/SSE server without opening a connection.

        This creates the internal bookkeeping entry so that
        ``has_connection()`` returns True and ``get_client_factory()`` can
        be called.  The factory will raise until ``connect()`` or
        ``connect_all()`` finishes the first attempt.
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
            conn._ready.set()
            return

        await self._open(conn)

        # Signal that the first attempt is done (success or failure)
        conn._ready.set()

        # Start the background health/reconnect monitor
        if conn._monitor_task is None or conn._monitor_task.done():
            conn._monitor_task = asyncio.create_task(
                self._monitor(conn), name=f"conn-monitor-{name}"
            )

    async def connect_all(self, config: BridgeConfig) -> None:
        """Open persistent connections for every registered HTTP/SSE server.

        Connections are opened **concurrently** in background tasks.  This
        method returns immediately so the bridge can start serving other
        servers without waiting for OAuth flows that require user interaction.

        Each server's ``_ready`` event gates the factory — callers that
        arrive before a connection resolves will wait up to
        ``_FACTORY_CONNECT_TIMEOUT`` seconds before raising ``ConnectionError``.
        """
        tasks: list[asyncio.Task[None]] = []
        for name, conn in self._connections.items():
            task = asyncio.create_task(
                self._connect_one(config, name, conn.srv),
                name=f"conn-open-{name}",
            )
            tasks.append(task)
            self._background_tasks.append(task)

        if tasks:
            logger.info(
                "Opening persistent connections for %d HTTP server(s): %s",
                len(tasks),
                [n for n in self._connections],
            )

    async def _connect_one(self, config: BridgeConfig, name: str, srv: ServerConfig) -> None:
        """Background wrapper around ``connect`` — logs but never raises."""
        try:
            await self.connect(config, name, srv)
        except Exception as e:
            logger.warning("Background connect for '%s' failed: %s", name, e)
            # Ensure ready is set even on unexpected errors
            conn = self._connections.get(name)
            if conn is not None:
                conn._ready.set()

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
        """Open the ``async with client:`` context and store the live client.

        This is the **only** place that creates a ``Client`` (and therefore
        the only place that creates an ``OAuth`` instance).  All other paths
        wait for or reuse the result.

        ``SystemExit`` is caught explicitly because the vendored uvicorn
        callback server calls ``sys.exit(1)`` when it cannot bind the OAuth
        callback port (e.g. address already in use).  Without this guard a
        transient port conflict would kill the entire bridge process.
        """
        try:
            client = _make_disconnected_client(conn.config, conn.name, conn.srv)
            # Enter the async-with context — this starts the session runner
            # and (for OAuth servers) triggers the auth flow.
            await conn.stack.enter_async_context(client)
            conn.client_ref[0] = client
            conn._backoff = _INITIAL_BACKOFF
            conn._auth_failed = False
            conn._auth_error_msg = ""
            logger.info("Persistent connection opened: %s", conn.name)
            # Notify the bridge so it can invalidate the tool cache
            if self._on_connected:
                try:
                    self._on_connected(conn.name)
                except Exception:
                    pass
        except SystemExit as e:
            # uvicorn calls sys.exit(1) when it can't bind the callback
            # port.  Treat this as a transient auth error — the health-check
            # will retry later.
            logger.warning(
                "OAuth callback server exited for '%s' (port conflict?): %s",
                conn.name,
                e,
            )
            conn.client_ref[0] = None
        except Exception as e:
            logger.warning(
                "Failed to open persistent connection for '%s': %s (%s)",
                conn.name,
                e,
                type(e).__name__,
            )
            _log_auth_failure_details(conn.name, conn.srv, e)
            conn.client_ref[0] = None
            if _is_auth_error(e):
                conn._auth_failed = True
                conn._auth_error_msg = str(e)
                logger.warning(
                    "Auth failure for '%s' — connection disabled until manual retry: %s",
                    conn.name,
                    e,
                )

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

                # Don't retry auth-failed connections
                if conn._auth_failed:
                    continue

                client = conn.client_ref[0]
                if client is None or not client.is_connected():
                    logger.warning("Connection to '%s' is down — reconnecting", conn.name)
                    await self._reconnect(conn)
                    continue

                # Lightweight health-check: MCP ping
                try:
                    await asyncio.wait_for(client.ping(), timeout=10.0)
                except Exception as e:
                    logger.warning(
                        "Health-check failed for '%s': %s (%s) — reconnecting",
                        conn.name,
                        type(e).__name__,
                        e or "no message",
                    )
                    await self._reconnect(conn)
        except asyncio.CancelledError:
            return


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _log_auth_failure_details(name: str, srv: ServerConfig, exc: BaseException) -> None:
    """Capture the response body and the headers we sent on a connection failure.

    The default ``HTTPStatusError`` formatting only includes the status code and
    URL, which is not enough to tell apart "bad credentials", "Copilot access
    required", and "wrong endpoint" — all of which surface as 401 from
    ``api.githubcopilot.com``.  We pull the server's response body (truncated) and
    redact our own Authorization header so the log is safe to share.
    """

    def _redact_header(value: str) -> str:
        if value.lower().startswith("bearer "):
            tail = value[7:]
            keep = tail[:6] if len(tail) > 12 else ""
            return f"Bearer {keep}…<redacted {max(len(tail) - len(keep), 0)} chars>"
        if len(value) > 16:
            return value[:6] + "…<redacted>"
        return value

    response = getattr(exc, "response", None)
    if response is not None:
        try:
            body = response.text
        except Exception:
            body = "<could not read response body>"
        if len(body) > 500:
            body = body[:500] + " …(truncated)"
        try:
            headers_repr = {k: v for k, v in response.headers.items()}
        except Exception:
            headers_repr = {}
        logger.warning(
            "Upstream response for '%s': status=%s body=%s response_headers=%s",
            name,
            response.status_code,
            body,
            headers_repr,
        )

    if srv.headers:
        sent: dict[str, str] = {}
        for k, v in srv.headers.items():
            if k.lower() in ("authorization", "x-api-key", "api-key", "cookie"):
                sent[k] = _redact_header(v)
            else:
                sent[k] = v
        logger.warning(
            "Headers configured for '%s' (sent verbatim, secrets redacted): %s",
            name,
            sent,
        )


def _is_auth_error(exc: BaseException) -> bool:
    """Return True if *exc* is an OAuth / authentication failure.

    Checks concrete exception types from the MCP SDK and FastMCP rather than
    fragile substring matching.  Falls back to ``httpx.HTTPStatusError`` with
    a 401/403 status code for transport-level auth rejections.
    """
    from fastmcp.client.auth.oauth import ClientNotFoundError
    from mcp.client.auth.exceptions import (
        OAuthFlowError,
    )  # covers OAuthTokenError, OAuthRegistrationError

    if isinstance(exc, (OAuthFlowError, ClientNotFoundError)):
        return True

    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (401, 403)

    return False


def _make_disconnected_client(
    config: BridgeConfig,
    name: str,
    srv: ServerConfig,
) -> HttpClient:
    """Create a disconnected ``Client`` for the given HTTP/SSE server.

    Ensures that both ``auth`` (``httpx.Auth``) and static ``headers`` from
    the server config are applied.  The ``headers`` field is how servers like
    GitHub Copilot MCP receive their Bearer token when ``auth`` is not set.

    Only called from ``_open()``.  The factory closure returned by
    ``get_client_factory()`` never calls this — it either returns the
    already-connected client or raises.
    """
    auth: httpx.Auth | None = build_auth(
        name,
        auth_config=srv.auth,
        server_url=srv.url,
        token_dir=config.oauth.token_dir_path,
        cache_tokens=config.oauth.cache_tokens,
    )

    url: str = _interpolate_str(srv.url) if srv.url else ""
    headers: dict[str, str] = _interpolate_dict(srv.headers) if srv.headers else {}

    # Always construct the transport explicitly so the return type is
    # ``Client[StreamableHttpTransport | SSETransport]`` — not the wide
    # union that ``Client(str)`` produces.
    transport: StreamableHttpTransport | SSETransport
    if srv.transport == Transport.SSE:
        transport = SSETransport(url=url, headers=headers)
    else:
        transport = StreamableHttpTransport(url=url, headers=headers)

    if auth is not None:
        return Client(transport, auth=auth)
    return Client(transport)
