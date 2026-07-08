"""FastMCP combiner server — proxies multiple MCP servers through one endpoint."""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal, overload

import httpx
from fastmcp import Client, FastMCP
from fastmcp.server import create_proxy
from fastmcp.server.middleware.error_handling import (
    ErrorHandlingMiddleware,
    RetryMiddleware,
)
from fastmcp.server.providers.proxy import FastMCPProxy
from fastmcp.tools import Tool
from mcp.server.session import ServerSession
from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp_combiner import nvim_proxy
from mcp_combiner.auth import (
    build_auth,
)
from mcp_combiner.config import (
    CombinerConfig,
    HealthResponse,
    ServerConfig,
    ServerStatusInfo,
    Transport,
    _interpolate_dict,  # noqa: PLC2701
    _interpolate_str,  # noqa: PLC2701
)
from mcp_combiner.connections import AuthenticationError, ConnectionManager
from mcp_combiner.middleware import ToolProcessingMiddleware  # noqa: F401 (re-export)
from mcp_combiner.mounts import mount_server_provider
from mcp_combiner.runtime import RUNTIME
from mcp_combiner.sharedserver import SharedServerManager
from mcp_combiner.toolcache import (  # noqa: F401 (re-exports for meta_tools/tests)
    UPSTREAM_TOOL_LIST_TIMEOUT,
    _filter_tools,
    _find_server_for_tool,
    _is_transport_dead,
    _matches_filter,
    _merge_stale_server_tools,
    _notify_session_by_id,
    _notify_tool_list_changed,
    _on_upstream_tools_ready,
    _partition_by_server,
    clear_tool_cache,
    invalidate_tool_cache,
    prime_server_tools,
    spawn_prime,
)

logger = logging.getLogger("mcp-combiner")

# Explicit re-export surface: names that historically lived in server.py and
# are imported from here by meta_tools (lazily) and the tests. Their homes are
# now toolcache.py / middleware.py; the bindings above keep this module a
# stable import point during the decomposition.
__all__ = [
    "ToolProcessingMiddleware",
    "build_server_status",
    "clear_tool_cache",
    "create_combiner",
    "invalidate_tool_cache",
    "prime_server_tools",
    "spawn_prime",
]

# NOTE (decomposition in progress): the mutable containers below are OWNED by
# mcp_combiner.runtime.RUNTIME; the historical module-global names are aliases
# to the same objects so existing cross-module imports keep observing the same
# state. Scalars (reassigned via ``global``) migrate to RUNTIME attributes as
# their code moves into the extracted modules.

# Track failed servers to avoid repeated errors
_failed_servers: dict[str, str] = RUNTIME.tools.failed_servers  # server_name -> error message

# Persistent connection manager for HTTP/SSE upstreams
_conn_manager: ConnectionManager | None = None

# The combiner instance, late-bound in create_combiner. Needed by sync
# callbacks (ConnectionManager.on_tools_ready) that prime through the mounted
# providers but are constructed before the combiner exists.
_combiner_instance: FastMCP | None = None

# Keep strong references to background prime tasks (asyncio only holds weak
# ones); the done-callback discards them.
_prime_tasks: set[asyncio.Task[Any]] = RUNTIME.prime_tasks

# Per-server "last-known-good" tool slices, keyed by server name, plus the
# wall-clock time each slice was last seen live. These let a server's tools
# survive a *transient* upstream drop (health-check reconnect, a dev restart)
# instead of vanishing from tools/list the instant it goes down.
#
# Why this exists: the tool set a client sees is derived, with no hysteresis,
# from whichever servers return tools in the current fetch. Because any one
# server's reconnect invalidates the whole cache and forces a refetch, a second
# server that happens to be mid-reconnect at that moment contributes zero tools
# and silently drops out — even though nothing changed for it (cross-server
# contamination). Re-injecting its last-known slice within a short grace window
# breaks that coupling and stops the green/red flapping.
#
# Slices are only re-served for servers that are configured, not disabled, and
# not auth-failed — a genuinely gone server (disabled/removed/auth-failed) is
# dropped and evicted, so we never advertise tools that can't be called.
_server_tool_cache: dict[str, list[Tool]] = RUNTIME.tools.server_tools
_server_tool_seen: dict[str, float] = RUNTIME.tools.server_seen

# Tools-ready state for stdio / sharedserver servers — those WITHOUT a persistent
# HTTP connection tracked by ConnectionManager. This mirrors ConnectionManager's
# ``_tools_ready`` so a mounted proxy gets the SAME started → ready tri-state: a
# freshly mounted or restarted proxy is "started" (process/proxy up, tools not
# yet confirmed) — NOT assumed ready — until a tools/list confirms its tools are
# listable. Set True when the server appears in a fresh fetch
# (_merge_stale_server_tools) or a priming list succeeds (prime_server_tools);
# dropped when the server is evicted from the slice cache (_merge_stale_server_tools).
_local_tools_ready: dict[str, bool] = RUNTIME.tools.local_tools_ready


# --- Session registry for ToolListChanged notifications ---
# Weak references to all active ServerSessions connected to this combiner.
# Populated by ToolProcessingMiddleware on each request; entries are
# automatically removed when the session is garbage-collected.
_active_sessions: weakref.WeakSet[ServerSession] = RUNTIME.sessions.active

# Per-session server blocklist.
# Maps a session ID string to the set of server names disabled for that session.
# Entries are explicitly removed via the /sessions/{id}/filter DELETE endpoint
# or by the meta-tools.  The REST API also supports external management by
# session ID (used by the Neovim plugin for ACP session filtering).
_session_disabled: dict[str, set[str]] = RUNTIME.sessions.disabled

# Token registry: token -> combiner session_id.
# The Neovim plugin generates a UUID token per chat and embeds it as the MCP
# URL path (/mcp/<token>).  TokenRewriteMiddleware rewrites the path to /mcp
# and records token -> mcp-session-id from the FastMCP response header.
# GET /sessions/token/{token} lets the Lua side look up the combiner session_id.
_token_sessions: dict[str, str] = RUNTIME.sessions.token_sessions

# Pending token filters: token -> set of disabled server names.
# Stored when Lua POSTs a filter before the remote client has connected
# (i.e. before the token is mapped to a session_id).  Applied immediately
# by TokenRewriteMiddleware when the token is first seen.
_pending_token_filters: dict[str, set[str]] = RUNTIME.sessions.pending_token_filters

# Neovim back-channel routing (tables, virtual-tool injection, REST routes) lives
# in mcp_combiner.nvim_proxy. server.py only calls its entry points.

# Unique per-process id, surfaced via /health. A change signals a combiner restart
# so clients re-register their Neovim instances and token bindings.
_COMBINER_BOOT_ID = RUNTIME.boot_id


# Strong references to in-flight notification tasks so they aren't GC'd
# before completion.
_notification_tasks: set[asyncio.Task[None]] = RUNTIME.notification_tasks


# Global config reference for tool filtering
_combiner_config: CombinerConfig | None = None


def build_server_status(
    config: CombinerConfig,
    conn_manager: ConnectionManager | None,
    name: str,
) -> ServerStatusInfo:
    """Single source of truth for a server's status snapshot + lifecycle state.

    Used by BOTH the ``/health`` endpoint (→ MCPStatus in Neovim) and the
    ``combiner__status`` meta-tool (→ self-reporting to the agent), so the two
    never diverge. Config fields come from ``CombinerConfig``; the runtime
    ``state`` is overlaid from the ``ConnectionManager`` for HTTP servers, or
    inferred for stdio/local servers (mounted ⇒ available ⇒ ``ready``).
    """
    info = config.get_server_status(name)
    srv = config.servers[name]
    failed = name in _failed_servers
    if srv.disabled:
        state = "disabled"
    elif conn_manager is not None and conn_manager.has_connection(name):
        state = conn_manager.lifecycle_state(name)
        # A recorded call failure (transport/process death) overrides an
        # optimistic connection-derived "ready"/"connected".
        if failed and state in ("ready", "connected"):
            state = "disconnected"
    elif failed:
        # stdio / non-connection-managed server whose last call failed — its
        # subprocess has crashed. No connection lifecycle tracks this, so the
        # failure mark is the down signal until a successful call clears it.
        state = "disconnected"
    elif _local_tools_ready.get(name):
        # stdio / non-connection-managed server whose tools are confirmed
        # listable (seen in a fetch, or a priming list succeeded).
        state = "ready"
    else:
        # Mounted/started but tools not yet confirmed — the same "connected"
        # (warming) rung of the tri-state HTTP servers get before "ready". We do
        # NOT assume ready just because it is mounted.
        state = "connected"
    return info.model_copy(update={"state": state})


def _effective_isolate(srv: ServerConfig) -> bool:
    """Resolve the tri-state ``isolate`` into the actual per-chat-session decision.

    ``isolate`` is ``None`` (absent → off), ``True`` (forced on) or ``False``
    (forced off). Per-chat isolation only applies to HTTP/SSE servers; stdio is
    never isolated (an explicit ``true`` there can't be honoured — it would need
    a subprocess per chat — so the caller logs a warning).
    """
    if not srv.isolate:
        return False
    if not ConnectionManager.is_http_server(srv):
        return False  # stdio: cannot isolate (caller warns)
    return True


def _create_server_proxy(config: CombinerConfig, name: str, srv: ServerConfig) -> FastMCP:
    """Create a proxy for a single upstream MCP server.

    When a persistent connection is available (HTTP/SSE servers), the proxy
    uses the connection manager's factory which returns the *already-connected*
    client — avoiding a connect/disconnect cycle per tool call.

    When the server has auth configured but no persistent connection, we
    create a ``Client`` with ``auth=`` set so the proxy's upstream HTTP
    requests carry the right credentials.

    For servers without auth and without a persistent connection we fall
    back to the simpler dict-based ``create_proxy(config_dict)`` path.

    Per-chat isolation (``isolate: true``, HTTP/SSE only): instead of one shared
    upstream session, a ``StatefulProxyClient`` opens a distinct upstream session
    per downstream chat (one server instance, shared transport), so a stateful
    server is handed a unique ``Mcp-Session-Id`` per chat. See ``_create_isolated_proxy``.
    """
    if _effective_isolate(srv):
        return _create_isolated_proxy(config, name, srv)

    # Prefer persistent connection if available
    if _conn_manager and _conn_manager.has_connection(name):
        factory = _conn_manager.get_client_factory(name)

        return FastMCPProxy(client_factory=factory, name=name)

    auth: httpx.Auth | None = build_auth(
        name,
        auth_config=srv.auth,
        server_url=srv.url,
        token_dir=config.oauth.token_dir_path,
        cache_tokens=config.oauth.cache_tokens,
    )

    if auth is not None and srv.url:
        # Auth requires a Client so we can inject httpx.Auth into the transport.
        # Always construct transport explicitly for a precise return type.
        from fastmcp.client.transports.http import StreamableHttpTransport
        from fastmcp.client.transports.sse import SSETransport

        url = _interpolate_str(srv.url)
        headers = _interpolate_dict(srv.headers) if srv.headers else {}

        transport: StreamableHttpTransport | SSETransport
        if srv.transport == Transport.SSE:
            transport = SSETransport(url=url, headers=headers)
        else:
            transport = StreamableHttpTransport(url=url, headers=headers)
        client = Client(transport, auth=auth)
        return create_proxy(client, name=name)

    # No auth — use the standard config-dict path (preserves headers)
    proxy_config = config.to_fastmcp_config(name)
    return create_proxy(proxy_config.model_dump(exclude_none=True), name=name)


def _create_isolated_proxy(config: CombinerConfig, name: str, srv: ServerConfig) -> FastMCP:
    """Build a per-chat-isolated proxy for an HTTP/SSE server (``isolate: true``).

    Uses FastMCP's ``StatefulProxyClient``: a single base client (one transport,
    one server instance) whose ``new_stateful`` factory opens — and caches — a
    *separate upstream session per downstream chat*. The upstream server thus
    assigns a distinct, stable ``Mcp-Session-Id`` per chat, so a stateful server
    (e.g. svg-mcp's current document) partitions its state per chat with no
    clash between concurrent chats. The per-chat session is force-disconnected
    when the downstream session ends; an abandoned one is just an idle upstream
    HTTP session the server can expire — never a leaked process.

    OAuth servers (``isolate: true`` + ``auth: oauth``): the per-chat sessions
    share the auth object owned by the ``ConnectionManager`` "primer" connection
    (see ``_lifespan``), so the OAuth flow runs *once* on the primer (one browser
    window), the primer keeps the token refreshed, and per-chat sessions reuse it
    — distinct ``Mcp-Session-Id`` per chat, single credential. The per-chat
    factory gates on the primer's readiness so concurrent first-use chats never
    race into parallel auth flows, and surfaces the primer's auth failure as a
    retryable ``AuthenticationError``.
    """
    from fastmcp.client.transports.http import StreamableHttpTransport
    from fastmcp.client.transports.sse import SSETransport
    from fastmcp.server.providers.proxy import StatefulProxyClient

    url = _interpolate_str(srv.url) if srv.url else ""
    headers = _interpolate_dict(srv.headers) if srv.headers else {}

    transport: StreamableHttpTransport | SSETransport
    if srv.transport == Transport.SSE:
        transport = SSETransport(url=url, headers=headers)
    else:
        transport = StreamableHttpTransport(url=url, headers=headers)

    # OAuth-isolated: borrow the primer connection's shared, refreshed auth and
    # gate per-chat session creation on the primer's eager auth completing.
    if _needs_oauth(srv) and _conn_manager is not None and _conn_manager.has_connection(name):
        mgr = _conn_manager
        shared_auth = mgr.get_auth(name)
        oauth_stateful: StatefulProxyClient[Any] = (
            StatefulProxyClient(transport, auth=shared_auth)
            if shared_auth is not None
            else StatefulProxyClient(transport)
        )

        async def _gated_factory() -> Any:
            if mgr.is_auth_failed(name):
                raise AuthenticationError(
                    f"Server '{name}' is disabled due to an authentication error: "
                    f"{mgr.auth_error(name)}. Use combiner__enable_server to retry."
                )
            # Wait for the primer's first connect (and thus its OAuth flow) to
            # finish before opening this chat's session, so the token already
            # exists and concurrent first-use chats don't trigger parallel flows.
            await mgr.wait_ready(name)
            if mgr.is_auth_failed(name):
                raise AuthenticationError(
                    f"Server '{name}' is disabled due to an authentication error: "
                    f"{mgr.auth_error(name)}. Use combiner__enable_server to retry."
                )
            return oauth_stateful.new_stateful()

        logger.info(
            "Server '%s': per-chat session isolation enabled "
            "(isolate=true, OAuth shared via primer connection)",
            name,
        )
        return FastMCPProxy(client_factory=_gated_factory, name=name)

    # Non-OAuth (or static-header/bearer auth): self-contained. The auth, if any,
    # is held on the shared base client so per-chat sessions reuse it.
    auth: httpx.Auth | None = build_auth(
        name,
        auth_config=srv.auth,
        server_url=srv.url,
        token_dir=config.oauth.token_dir_path,
        cache_tokens=config.oauth.cache_tokens,
    )
    stateful: StatefulProxyClient[Any] = (
        StatefulProxyClient(transport, auth=auth)
        if auth is not None
        else StatefulProxyClient(transport)
    )
    logger.info("Server '%s': per-chat session isolation enabled (isolate=true)", name)
    return FastMCPProxy(client_factory=stateful.new_stateful, name=name)


def _needs_oauth(srv: ServerConfig) -> bool:
    """Check if a server requires OAuth authentication."""
    if srv.auth == "oauth":
        return True
    if isinstance(srv.auth, dict) and "oauth" in srv.auth:
        return True
    return False


@overload
def create_combiner(
    config_path: str,
    *,
    oauth_cache_tokens: bool | None = ...,
    oauth_token_dir: str | None = ...,
    normalize_schemas: bool = ...,
    schema_fixes: frozenset[str] | None = ...,
    input_validation: bool | None = ...,
    output_validation: bool | None = ...,
    stale_tool_grace: float | None = ...,
    return_ss_manager: Literal[True],
) -> tuple[FastMCP, SharedServerManager]: ...


@overload
def create_combiner(
    config_path: str,
    *,
    oauth_cache_tokens: bool | None = ...,
    oauth_token_dir: str | None = ...,
    normalize_schemas: bool = ...,
    schema_fixes: frozenset[str] | None = ...,
    input_validation: bool | None = ...,
    output_validation: bool | None = ...,
    stale_tool_grace: float | None = ...,
    return_ss_manager: Literal[False] = ...,
) -> FastMCP: ...


def create_combiner(
    config_path: str,
    *,
    oauth_cache_tokens: bool | None = None,
    oauth_token_dir: str | None = None,
    normalize_schemas: bool = False,
    schema_fixes: frozenset[str] | None = None,
    input_validation: bool | None = None,
    output_validation: bool | None = None,
    stale_tool_grace: float | None = None,
    return_ss_manager: bool = False,
) -> FastMCP | tuple[FastMCP, SharedServerManager]:
    """Create the combiner FastMCP server from a config file.

    Reads servers.json, creates a proxy for each enabled server,
    mounts them under namespaced prefixes, and adds meta-tools + health.

    Startup semantics for HTTP/OAuth servers:

    * Every enabled server is **mounted immediately** (proxy created).
    * HTTP/SSE servers are registered with the ``ConnectionManager``.
    * ``connect_all()`` opens persistent connections in **background tasks
      and returns immediately** — it does not wait for them to finish.  The
      combiner starts serving right away so that servers needing OAuth (which
      may block on a browser flow) don't hold up the rest.  Their tools
      appear once their tools are listable: each ``on_tools_ready`` callback
      invalidates the tool cache and emits ``notifications/tools/list_changed``
      so clients re-fetch and pick up the newly available tools. (The earlier
      ``on_connection_success`` lifecycle event does not invalidate — the
      session can be established before the upstream can answer ``tools/list``.)
    * Per-chat *isolated OAuth* servers are the exception: session creation
      gates on the primer's first connect attempt via ``wait_ready()``
      (60s timeout) before the session is opened.
    * If an OAuth server fails authentication, it is marked
      ``_auth_failed`` and the factory raises ``AuthenticationError``
      (not retried by ``RetryMiddleware``).  The auto-reconnect health
      monitor skips auth-failed servers, so recovery is manual via one of
      the meta-tools:
        - ``combiner__enable_server`` — re-arm a disabled or auth-failed
          server (clears the auth-failed flag and reconnects).
        - ``combiner__restart_server`` — kick a single wedged server (stale
          auth, hung, crashed subprocess): tears down + respawns just that
          server's process/connection, no full combiner restart.
        - ``combiner__reload_config`` — apply on-disk config changes without
          a restart.

    CLI overrides (when provided) take precedence over the ``oauth`` section
    of the config file:

    - *oauth_cache_tokens*: ``False`` disables disk token caching globally.
    - *oauth_token_dir*: path override for the OAuth token directory.

    If *return_ss_manager* is True, returns a tuple of (combiner, ss_manager)
    so the caller can explicitly call stop_all() on shutdown.
    """
    global _combiner_config
    global _conn_manager
    global _combiner_instance

    # Configurable stale-tool grace: how long a disconnected server keeps serving
    # its last-known tools before they're dropped. Default STALE_TOOL_GRACE.
    if stale_tool_grace is not None:
        RUNTIME.tools.stale_grace = float(stale_tool_grace)
        logger.info("Stale-tool grace set to %.0fs", RUNTIME.tools.stale_grace)

    # Replace the MCP SDK's per-call jsonschema.validate (which rebuilds the
    # validator + re-checks the meta-schema on every tool call) with a cached
    # validator. Idempotent; cache is cleared on reload via invalidate_tool_cache.
    from mcp_combiner import fastvalidate

    fastvalidate.install()
    # Tri-state output-schema validation (None = SDK default, True = force on,
    # False = force off). The upstream server already validated its structured
    # output, so forcing it off removes redundant per-call work at the proxy.
    # Input validation is driven natively via strict_input_validation below.
    fastvalidate.set_output_validation(output_validation)

    config = CombinerConfig.load(config_path)
    _combiner_config = config  # Store for tool filtering
    RUNTIME.config = config
    # ``--normalize-schema`` is a back-compat alias for the anyof_type_hoist fix.
    RUNTIME.schema_fixes = frozenset(schema_fixes or ()) | (
        frozenset({"anyof_type_hoist"}) if normalize_schemas else frozenset()
    )
    if RUNTIME.schema_fixes:
        logger.info("Schema fixes enabled for tools/list: %s", sorted(RUNTIME.schema_fixes))

    # Apply CLI overrides on top of config-file oauth settings
    if oauth_cache_tokens is not None:
        config.oauth.cache_tokens = oauth_cache_tokens
    if oauth_token_dir is not None:
        config.oauth.token_dir = oauth_token_dir

    ss_manager = SharedServerManager(config)
    conn_manager = ConnectionManager(
        # Connection-open is a lifecycle signal only — it does NOT mean the
        # upstream can list tools yet, so it must not invalidate the cache.
        on_connection_success=lambda name: logger.debug(
            "Upstream '%s' connected (session established)", name
        ),
        # Tools-ready is the correct trigger: the upstream's tools just proved
        # listable, so prime (store the slice) and broadcast — never earlier.
        on_tools_ready=_on_upstream_tools_ready,
        # A sharedserver-backed upstream whose process died has nobody to
        # respawn it — re-dialing alone spins forever. After repeated failed
        # reconnects the monitor escalates to this hard restart (the same
        # path combiner__restart_server takes manually).
        restart_backing=ss_manager.restart,
    )
    _conn_manager = conn_manager
    RUNTIME.conn_manager = conn_manager
    RUNTIME.ss_manager = ss_manager

    @asynccontextmanager
    async def _lifespan(server: FastMCP) -> AsyncIterator[None]:
        await ss_manager.start_all()

        # Mount every enabled server.  OAuth servers are mounted even if
        # they don't have a cached token — the persistent connection attempt
        # in connect_all() (below) handles the single auth flow.  If it
        # fails, ConnectionManager marks _auth_failed and the factory
        # raises AuthenticationError for all subsequent calls.
        enabled = config.get_enabled_servers()
        local_servers: list[str] = []
        for name, srv in enabled.items():
            # isolate is HTTP/SSE-only; an explicit true on a stdio server can't
            # be honoured (it would need a subprocess per chat).
            if srv.isolate and not conn_manager.is_http_server(srv):
                logger.warning(
                    "Server '%s': isolate=true ignored — only HTTP/SSE servers "
                    "support per-chat sessions (stdio has one session per process)",
                    name,
                )

            # Pre-register EVERY HTTP/SSE server for a persistent connection —
            # including isolated ones. For an isolated server the persistent
            # connection is a pure "primer": chats are NOT routed through it
            # (the mounted proxy opens per-chat sessions), but it drives the
            # whole lifecycle — the eager OAuth flow + token refresh (whose
            # auth OAuth per-chat sessions share), the tools-ready prime, the
            # health-check monitor, reconnect, and the backing-process restart
            # escalation. Previously non-OAuth isolated servers were skipped
            # here, which left them with NO lifecycle at all: nothing primed
            # them at startup and nothing monitored them.
            if conn_manager.is_http_server(srv):
                conn_manager.register(config, name, srv)

            try:
                proxy = _create_server_proxy(config, name, srv)
                mount_server_provider(server, proxy, name)
                logger.info("Mounted server: %s (%s)", name, srv.transport.value)
                # Only stdio servers need the explicit startup prime below —
                # every HTTP server (isolated included) now has a primer
                # connection whose connect drives its tools-ready prime.
                if not conn_manager.is_http_server(srv):
                    local_servers.append(name)
            except Exception:
                logger.exception("Failed to mount server '%s'", name)

        # Start persistent connections to HTTP/SSE upstreams in the background.
        # OAuth servers get exactly one auth attempt here.  The combiner starts
        # serving immediately; servers requiring OAuth are available once the
        # user completes the browser flow.  If auth fails the connection is
        # marked _auth_failed — no retry until manual toggle via combiner__enable_server.
        await conn_manager.connect_all(config)
        logger.info("Connection tasks started — combiner is ready")

        # Started → ready is not automatic: a mounted server sits at "started"
        # until a tools/list invocation answers. Connection-managed HTTP
        # servers get theirs from connect_all's connection probe
        # (→ _on_upstream_tools_ready). Servers WITHOUT a persistent
        # connection — stdio, and isolated non-OAuth HTTP — have no such
        # lifecycle, so kick off their primes here, the same
        # prime_server_tools path restart/enable use.
        for name in local_servers:
            spawn_prime(server, name)

        try:
            yield
        finally:
            for task in list(_prime_tasks):
                task.cancel()
            await conn_manager.close_all()
            await ss_manager.stop_all()

    combiner = FastMCP(
        name="mcp-combiner",
        instructions="MCP Combiner — proxies multiple MCP servers through a single endpoint.",
        # Tri-state input-schema validation. None → fastmcp's own default
        # (off — inputs are coerced, not strictly validated); True → force
        # strict validation on; False → force it off. This is the only switch
        # that actually gates the SDK's per-call input jsonschema.validate.
        strict_input_validation=input_validation,
        dereference_schemas=False,  # Disabled: circular $ref causes infinite recursion
        middleware=[
            # Outermost: catch-all safety net for any unhandled exception
            ErrorHandlingMiddleware(
                logger=logger,
                include_traceback=True,
            ),
            # Middle: retry transient upstream failures with exponential backoff
            RetryMiddleware(
                max_retries=2,
                retry_exceptions=(ConnectionError, TimeoutError),
                logger=logger,
            ),
            # Innermost: caching, filtering, sanitization, domain error handling
            ToolProcessingMiddleware(),
        ],
        lifespan=_lifespan,
    )

    # Register meta-tools (available immediately; server proxies mount in lifespan)
    from mcp_combiner.meta_tools import register_meta_tools

    register_meta_tools(combiner, config, conn_manager, ss_manager)
    _combiner_instance = combiner
    RUNTIME.combiner = combiner

    # Health endpoint
    @combiner.custom_route("/health", methods=["GET"])
    async def health_check(request: Request) -> JSONResponse:
        server_statuses: dict[str, ServerStatusInfo] = {
            name: build_server_status(config, conn_manager, name) for name in config.servers
        }
        auth_failed = [n for n in conn_manager._connections if conn_manager.is_auth_failed(n)]
        response = HealthResponse(
            status="ok",
            servers=server_statuses,
            config_path=config.config_path,
            pending_oauth=auth_failed,
        )
        payload = response.model_dump(mode="json")
        # boot_id changes only when this combiner *process* (re)starts. Clients use
        # it to detect a restart and re-register Neovim instances + token binds.
        payload["boot_id"] = _COMBINER_BOOT_ID
        return JSONResponse(payload)

    # --- Session management REST API ---
    # These endpoints allow external clients (e.g. the Neovim plugin) to
    # list active MCP sessions and manage per-session server filters by
    # session ID, without needing to be the session owner.

    @combiner.custom_route("/sessions", methods=["GET"])
    async def list_sessions(request: Request) -> JSONResponse:
        """List active MCP sessions with their IDs, client info, and filter state."""
        sessions_out: list[dict[str, Any]] = []
        for sess in list(_active_sessions):
            try:
                sid = getattr(sess, "_fastmcp_state_prefix", None) or str(id(sess))
            except AttributeError:
                sid = str(id(sess))
            blocked = _session_disabled.get(sid, set())
            # Extract client info from the MCP initialize handshake
            client_info: dict[str, Any] | None = None
            try:
                cp = getattr(sess, "client_params", None)
                ci = getattr(cp, "clientInfo", None) if cp else None
                if ci:
                    client_info = {
                        "name": getattr(ci, "name", None),
                        "version": getattr(ci, "version", None),
                    }
            except Exception:
                pass
            entry: dict[str, Any] = {
                "session_id": sid,
                "disabled_servers": sorted(blocked),
            }
            if client_info:
                entry["client_info"] = client_info
            sessions_out.append(entry)
        return JSONResponse({"sessions": sessions_out})

    @combiner.custom_route("/sessions/{session_id}/filter", methods=["GET", "POST", "DELETE"])
    async def manage_session_filter(request: Request) -> JSONResponse:
        """Manage per-session server blocklist by session ID.

        GET: Get current disabled servers for a session.
        POST: Set disabled servers for a session.
              Body: { "disabled_servers": ["server1", "server2"] }
              Or:   { "allowed_servers": ["server1"] } — inverts to disable all others
        DELETE: Clear all session filters for a session.
        """
        session_id = request.path_params.get("session_id", "")
        if not session_id:
            return JSONResponse({"error": "session_id required"}, status_code=400)

        if request.method == "GET":
            disabled = _session_disabled.get(session_id, set())
            return JSONResponse(
                {
                    "session_id": session_id,
                    "disabled_servers": sorted(disabled),
                }
            )

        if request.method == "DELETE":
            removed = _session_disabled.pop(session_id, None)
            # Notify the session so its tool list refreshes
            await _notify_session_by_id(session_id)
            return JSONResponse(
                {
                    "session_id": session_id,
                    "action": "cleared",
                    "previously_disabled": sorted(removed) if removed else [],
                }
            )

        # POST — manage disabled servers
        # Accepts:
        #   { "disabled_servers": ["srv1", "srv2"] } — set explicit disable list
        #   { "allowed_servers": ["srv1"] } — allow list, inverts to disable all others
        #   { "enable": "srv1" } — enable a single server (remove from disabled)
        #   { "disable": "srv1" } — disable a single server (add to disabled)
        # Note: allowed_servers=[] means disable ALL servers (not "allow all")
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        # Handle single-server toggle operations first
        enable_server = body.get("enable")
        disable_server = body.get("disable")

        if enable_server is not None:
            if enable_server not in config.servers:
                return JSONResponse({"error": f"Unknown server: {enable_server}"}, status_code=400)
            current = _session_disabled.get(session_id, set())
            current.discard(enable_server)
            if current:
                _session_disabled[session_id] = current
            else:
                _session_disabled.pop(session_id, None)
            await _notify_session_by_id(session_id)
            logger.info("REST: session %s enabled server %s", session_id, enable_server)
            return JSONResponse(
                {
                    "session_id": session_id,
                    "action": "enabled",
                    "server": enable_server,
                    "disabled_servers": sorted(_session_disabled.get(session_id, set())),
                }
            )

        if disable_server is not None:
            if disable_server not in config.servers:
                return JSONResponse({"error": f"Unknown server: {disable_server}"}, status_code=400)
            current = _session_disabled.setdefault(session_id, set())
            current.add(disable_server)
            await _notify_session_by_id(session_id)
            logger.info("REST: session %s disabled server %s", session_id, disable_server)
            return JSONResponse(
                {
                    "session_id": session_id,
                    "action": "disabled",
                    "server": disable_server,
                    "disabled_servers": sorted(_session_disabled.get(session_id, set())),
                }
            )

        # Bulk operations: allowed_servers or disabled_servers
        allowed = body.get("allowed_servers")
        disabled = body.get("disabled_servers")

        # If allowed_servers is provided, compute disabled as inverse
        if allowed is not None:
            if not isinstance(allowed, list):
                return JSONResponse({"error": "allowed_servers must be a list"}, status_code=400)
            allowed_set = set(allowed)
            # Disable all servers not in allowed list (except _combiner meta-server)
            disabled_list = [s for s in config.servers if s not in allowed_set and s != "_combiner"]
        elif disabled is None:
            disabled_list = []
        else:
            disabled_list = list(disabled) if isinstance(disabled, list) else []

        if not isinstance(disabled_list, list):
            return JSONResponse({"error": "disabled_servers must be a list"}, status_code=400)

        # Validate server names
        unknown = [s for s in disabled_list if s not in config.servers]
        if unknown:
            return JSONResponse({"error": f"Unknown servers: {unknown}"}, status_code=400)

        if disabled_list:
            _session_disabled[session_id] = set(disabled_list)
        else:
            _session_disabled.pop(session_id, None)

        # Notify the target session
        await _notify_session_by_id(session_id)

        logger.info(
            "REST: session %s filter set to disabled=%s",
            session_id,
            disabled_list,
        )
        return JSONResponse(
            {
                "session_id": session_id,
                "disabled_servers": sorted(_session_disabled.get(session_id, set())),
            }
        )

    @combiner.custom_route("/sessions/token/{token}", methods=["GET"])
    async def lookup_session_token(request: Request) -> JSONResponse:
        """Look up the combiner session_id associated with a token.

        The token is a UUID generated by the Neovim plugin per chat and
        embedded as the URL path suffix (/mcp/<token>).
        TokenRewriteMiddleware records the mapping when FastMCP assigns the
        session_id on the first initialize response.
        """
        token = request.path_params.get("token", "")
        session_id = _token_sessions.get(token)
        if session_id is None:
            return JSONResponse({"error": "token not found"}, status_code=404)
        logger.debug("Token lookup: %s -> %s", token, session_id)
        return JSONResponse({"token": token, "session_id": session_id})

    @combiner.custom_route("/sessions/token/{token}/filter", methods=["GET", "POST", "DELETE"])
    async def manage_token_filter(request: Request) -> JSONResponse:
        """Manage per-session server blocklist by token.

        The token is the stable identifier the Lua plugin holds for both ACP
        and HTTP adapter sessions.  If the token is already mapped to a
        session_id the operation is applied immediately; otherwise it is stored
        as pending and applied by TokenRewriteMiddleware when the client connects.

        GET:    Returns current or pending filter state.
        POST:   Same body format as /sessions/{session_id}/filter.
                If the session is not yet connected, stores as pending.
        DELETE: Clears filter (and any pending state).
        """
        token = request.path_params.get("token", "")
        if not token:
            return JSONResponse({"error": "token required"}, status_code=400)

        session_id = _token_sessions.get(token)

        if request.method == "GET":
            if session_id:
                disabled = _session_disabled.get(session_id, set())
                return JSONResponse(
                    {"token": token, "session_id": session_id, "disabled_servers": sorted(disabled)}
                )
            pending = _pending_token_filters.get(token, set())
            return JSONResponse(
                {
                    "token": token,
                    "session_id": None,
                    "pending": True,
                    "disabled_servers": sorted(pending),
                }
            )

        if request.method == "DELETE":
            _pending_token_filters.pop(token, None)
            if session_id:
                removed = _session_disabled.pop(session_id, None)
                await _notify_session_by_id(session_id)
                return JSONResponse(
                    {
                        "token": token,
                        "session_id": session_id,
                        "action": "cleared",
                        "previously_disabled": sorted(removed) if removed else [],
                    }
                )
            return JSONResponse({"token": token, "session_id": None, "action": "cleared"})

        # POST — parse body (same format as /sessions/{id}/filter)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        # Resolve to a disabled set using the same logic as manage_session_filter
        enable_server = body.get("enable")
        disable_server = body.get("disable")
        allowed = body.get("allowed_servers")
        disabled_list = body.get("disabled_servers")

        def _resolve_disabled(current: set[str]) -> set[str] | None:
            """Return new disabled set or None to clear."""
            if enable_server is not None:
                current.discard(enable_server)
                return current if current else None
            if disable_server is not None:
                current.add(disable_server)
                return current
            if allowed is not None:
                allowed_set = set(allowed)
                d = {s for s in config.servers if s not in allowed_set and s != "_combiner"}
                return d if d else None
            if disabled_list is not None:
                return set(disabled_list) if disabled_list else None
            return current if current else None

        if session_id:
            # Session already connected — apply immediately
            current = set(_session_disabled.get(session_id, set()))
            new_disabled = _resolve_disabled(current)
            if new_disabled:
                _session_disabled[session_id] = new_disabled
            else:
                _session_disabled.pop(session_id, None)
            await _notify_session_by_id(session_id)
            logger.info(
                "REST token filter: token=%s session=%s disabled=%s",
                token,
                session_id,
                sorted(_session_disabled.get(session_id, set())),
            )
            return JSONResponse(
                {
                    "token": token,
                    "session_id": session_id,
                    "disabled_servers": sorted(_session_disabled.get(session_id, set())),
                }
            )

        # Session not yet connected — store as pending
        current = set(_pending_token_filters.get(token, set()))
        new_disabled = _resolve_disabled(current)
        if new_disabled:
            _pending_token_filters[token] = new_disabled
        else:
            _pending_token_filters.pop(token, None)
        logger.info(
            "REST token filter (pending): token=%s disabled=%s",
            token,
            sorted(new_disabled) if new_disabled else [],
        )
        return JSONResponse(
            {
                "token": token,
                "session_id": None,
                "pending": True,
                "disabled_servers": sorted(new_disabled) if new_disabled else [],
            }
        )

    # Neovim back-channel REST API (/neovim/instances, /neovim/bind).
    nvim_proxy.register_routes(combiner, _notify_tool_list_changed)

    if return_ss_manager:
        return combiner, ss_manager
    return combiner
