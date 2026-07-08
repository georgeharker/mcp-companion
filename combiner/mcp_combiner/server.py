"""FastMCP combiner server — proxies multiple MCP servers through one endpoint."""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal, overload

from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import (
    ErrorHandlingMiddleware,
    RetryMiddleware,
)
from fastmcp.tools import Tool
from mcp.server.session import ServerSession

from mcp_combiner import nvim_proxy
from mcp_combiner.config import (
    CombinerConfig,  # noqa: PLC2701
)
from mcp_combiner.connections import ConnectionManager
from mcp_combiner.middleware import ToolProcessingMiddleware  # noqa: F401 (re-export)
from mcp_combiner.mounts import mount_server_provider
from mcp_combiner.proxyfactory import (
    _create_server_proxy as _create_server_proxy,  # noqa: PLC0414 (re-export)
)
from mcp_combiner.routes import register_routes
from mcp_combiner.runtime import RUNTIME
from mcp_combiner.sharedserver import SharedServerManager
from mcp_combiner.status import build_server_status as build_server_status  # noqa: PLC0414
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

    # Session/health REST API (extracted to routes.py).
    register_routes(combiner, config, conn_manager)

    # Neovim back-channel REST API (/neovim/instances, /neovim/bind).
    nvim_proxy.register_routes(combiner, _notify_tool_list_changed)

    if return_ss_manager:
        return combiner, ss_manager
    return combiner
