"""FastMCP bridge server — proxies multiple MCP servers through one endpoint."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import weakref
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any, Literal, overload

import httpx
import mcp.types as mt
from fastmcp import Client, FastMCP
from fastmcp.exceptions import NotFoundError, ToolError
from fastmcp.server import create_proxy
from fastmcp.server.providers.proxy import FastMCPProxy
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.middleware.error_handling import (
    ErrorHandlingMiddleware,
    RetryMiddleware,
)
from fastmcp.tools import Tool
from fastmcp.tools.tool import ToolResult
from mcp.server.session import ServerSession
from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp_bridge.auth import (
    build_auth,
    clear_oauth_cache,
    is_stale_client_error,
)
from mcp_bridge.config import (
    BridgeConfig,
    HealthResponse,
    ServerConfig,
    ServerStatusInfo,
    Transport,
    _interpolate_dict,  # noqa: PLC2701
    _interpolate_str,  # noqa: PLC2701
)
from mcp_bridge.connections import AuthenticationError, ConnectionManager
from mcp_bridge.sharedserver import SharedServerManager

logger = logging.getLogger("mcp-bridge")

# Track failed servers to avoid repeated errors
_failed_servers: dict[str, str] = {}  # server_name -> error message

# Persistent connection manager for HTTP/SSE upstreams
_conn_manager: ConnectionManager | None = None

# Timeout for individual upstream server queries during tools/list
UPSTREAM_TOOL_LIST_TIMEOUT = 5.0  # seconds


# Global tool cache - shared across middleware instances
_tool_cache: list[Tool] | None = None
_tool_cache_time: float = 0

# --- Session registry for ToolListChanged notifications ---
# Weak references to all active ServerSessions connected to this bridge.
# Populated by ToolProcessingMiddleware on each request; entries are
# automatically removed when the session is garbage-collected.
_active_sessions: weakref.WeakSet[ServerSession] = weakref.WeakSet()

# Strong references to in-flight notification tasks so they aren't GC'd
# before completion.
_notification_tasks: set[asyncio.Task[None]] = set()


async def _notify_tool_list_changed() -> None:
    """Send ``notifications/tools/list_changed`` to every active MCP session.

    Exceptions from individual sessions (e.g. client already disconnected)
    are logged and swallowed so one bad session never blocks the rest.
    """
    sessions = list(_active_sessions)
    if not sessions:
        logger.debug("No active sessions to notify of tool list change")
        return

    logger.info("Notifying %d active session(s) of tool list change", len(sessions))
    for session in sessions:
        try:
            await session.send_tool_list_changed()
        except Exception:
            logger.debug("Failed to notify session of tool list change", exc_info=True)


# Global config reference for tool filtering
_bridge_config: BridgeConfig | None = None


def _matches_filter(tool_name: str, patterns: list[str]) -> bool:
    """Check if a tool name matches any of the glob patterns."""
    import fnmatch

    for pattern in patterns:
        if fnmatch.fnmatch(tool_name, pattern):
            return True
    return False


def _find_server_for_tool(tool_name: str) -> tuple[str | None, str]:
    """Find which server a tool belongs to based on its name prefix.

    Returns (server_name, local_tool_name) or (None, tool_name) if no match.
    FastMCP namespaces tools as "servername_toolname" with single underscore.
    """
    if _bridge_config is None:
        return None, tool_name

    # Check each server name to see if the tool starts with it
    for server_name in _bridge_config.servers:
        prefix = server_name + "_"
        if tool_name.startswith(prefix):
            local_name = tool_name[len(prefix) :]
            return server_name, local_name

    return None, tool_name


def _filter_tools(tools: list[Tool]) -> list[Tool]:
    """Filter tools based on server-specific tool_filter patterns."""
    if _bridge_config is None:
        return tools

    filtered: list[Tool] = []
    for tool in tools:
        name = str(tool.name) if tool.name else ""

        server_name, local_name = _find_server_for_tool(name)

        if server_name is None:
            # Bridge tools (no server prefix) - always include
            filtered.append(tool)
            continue

        # Get server config
        srv = _bridge_config.servers.get(server_name)
        if srv is None or not srv.tool_filter:
            # No filter configured - include all tools from this server
            filtered.append(tool)
        elif _matches_filter(local_name, srv.tool_filter):
            # Matches filter - include
            filtered.append(tool)
        # else: doesn't match filter - exclude

    return filtered


def invalidate_tool_cache() -> None:
    """Invalidate the tool cache, forcing a refresh on next tools/list.

    Also sends ``notifications/tools/list_changed`` to all connected MCP
    clients so they re-fetch the tool list immediately.
    """
    global _tool_cache, _tool_cache_time
    _tool_cache = None
    _tool_cache_time = 0
    logger.info("Tool cache invalidated")

    # Fire-and-forget notification to all connected sessions.
    # We schedule this as a task because invalidate_tool_cache() is called
    # from sync contexts (e.g. ConnectionManager.on_connected callback).
    # The task is stored in _notification_tasks to prevent GC before completion.
    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(_notify_tool_list_changed())
        _notification_tasks.add(task)
        task.add_done_callback(_notification_tasks.discard)
    except RuntimeError:
        # No running event loop — skip notification (e.g. during tests)
        pass


def _safe_json_clone(obj: object) -> Any:
    """JSON round-trip to break Python-level circular object identity."""
    return json.loads(json.dumps(obj, default=str))


class ToolProcessingMiddleware(Middleware):
    """Intercept tools/list with caching and sanitization.

    Caching: Tool lists are cached globally and only refreshed when:
    - Cache is empty (first request)
    - Cache was explicitly invalidated (server enable/disable)
    - Cache is older than 5 minutes (safety refresh)

    This dramatically improves tools/list performance by avoiding
    re-querying all upstream servers on every request.

    Sanitization: FastMCP ProxyTool objects can carry circular Python
    object references (especially from servers with $ref schemas like
    Todoist). Pydantic's ``model_dump()`` crashes with 'Circular
    reference detected (id repeated)'. We catch these and rebuild as
    clean FunctionTools.
    """

    CACHE_TTL = 300  # 5 minutes max cache age

    async def on_request(
        self,
        context: MiddlewareContext[mt.Request[Any, Any]],
        call_next: CallNext[mt.Request[Any, Any], Any],
    ) -> Any:
        """Track active sessions for ToolListChanged notifications."""
        if context.fastmcp_context is not None:
            try:
                session = context.fastmcp_context.session
                _active_sessions.add(session)
            except (RuntimeError, AttributeError):
                pass  # Session not yet established
        return await call_next(context)

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        global _tool_cache, _tool_cache_time

        now = time.time()
        cache_age = now - _tool_cache_time

        # Use cache if valid
        if _tool_cache is not None and cache_age < self.CACHE_TTL:
            logger.warning(
                "tools/list: CACHE HIT (%d tools, %.1fs old)",
                len(_tool_cache),
                cache_age,
            )
            return _tool_cache

        # Cache miss or expired - fetch fresh
        logger.warning("tools/list: CACHE MISS - fetching fresh (cache_age=%.1fs)", cache_age)
        try:
            tools = list(await call_next(context))
        except Exception as e:
            # If fetching fails, return stale cache or empty list
            # This prevents one failing server from breaking tools/list entirely
            logger.error("tools/list: upstream error, returning stale cache: %s", e)
            if _tool_cache is not None:
                return _tool_cache
            return []

        # Sanitize and cache
        sanitized: list[Tool] = []
        for tool in tools:
            try:
                tool.model_dump(by_alias=True, mode="json", exclude_none=True)
                sanitized.append(tool)
            except (ValueError, RecursionError):
                logger.warning("Replacing circular tool: %s", tool.name)
                sanitized.append(self._to_clean_tool(tool))

        # Apply tool filters from server configs
        filtered = _filter_tools(sanitized)
        if len(filtered) < len(sanitized):
            logger.info(
                "tools/list: filtered %d -> %d tools based on tool_filter",
                len(sanitized),
                len(filtered),
            )

        _tool_cache = filtered
        _tool_cache_time = now
        logger.info("tools/list: cached %d tools", len(filtered))

        return filtered

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """Wrap tool calls with error handling for resilience.

        Error strategy:
        - NotFoundError (unknown/disabled tool): re-raised as a protocol error
          (-32002). This is a client mistake — the tool name is wrong or the
          server is disabled. The AI should not retry with the same name.
        - ToolError already raised upstream: re-raised unchanged so FastMCP
          converts it to CallToolResult(isError=True) correctly.
        - All other exceptions (connection, auth, rate-limit, etc.): wrapped
          as ToolError so FastMCP sets isError=True in the response. This is
          the correct MCP semantics: "the tool ran but something went wrong".
        """
        tool_name = context.message.name if context.message else "unknown"
        try:
            return await call_next(context)
        except NotFoundError:
            # Protocol error — wrong tool name or server disabled. Re-raise
            # so the MCP layer returns a -32002 JSON-RPC error, not a tool result.
            raise
        except ToolError:
            # Already a proper tool error — re-raise unchanged.
            raise
        except AuthenticationError as e:
            # Auth-failed servers: convert to ToolError immediately.
            # This must NOT propagate as a generic exception — RetryMiddleware
            # would catch it and retry (creating new OAuth instances).
            logger.warning("Tool '%s' blocked by auth failure: %s", tool_name, e)
            raise ToolError(
                f"Tool '{tool_name}' is unavailable — the server's authentication "
                f"failed. Use bridge__enable_server to retry authentication."
            ) from e
        except Exception as e:
            # Extract server name by stripping the known namespace prefix.
            # FastMCP namespaces as "servername_toolname"; longest match wins
            # to handle server names that are prefixes of each other.
            server_name: str | None = None
            if _bridge_config:
                for sname in sorted(_bridge_config.servers, key=len, reverse=True):
                    if tool_name.startswith(sname + "_"):
                        server_name = sname
                        break

            error_str = str(e)

            # Check for rate limiting (429) — transient, caller should retry
            if (
                "429" in error_str
                or "too many requests" in error_str.lower()
                or "rate limit" in error_str.lower()
            ):
                logger.warning("Tool '%s' rate-limited (429): %s", tool_name, e)
                raise ToolError(
                    f"Tool '{tool_name}' is temporarily unavailable due to rate limiting "
                    f"(HTTP 429). Please wait a moment and retry."
                ) from e

            # Check if this is a stale OAuth error — clear cache so next
            # attempt triggers fresh authentication
            if server_name and is_stale_client_error(e):
                logger.warning(
                    "Tool '%s' failed with stale OAuth error, clearing cache for '%s': %s",
                    tool_name,
                    server_name,
                    e,
                )
                from mcp_bridge.config import OAuthConfig

                token_dir = OAuthConfig().token_dir_path
                clear_oauth_cache(server_name, token_dir)
                _failed_servers[server_name] = f"OAuth error: {e}"

            logger.error("Tool '%s' failed: %s", tool_name, e)
            raise ToolError(f"Error calling tool '{tool_name}': {e}") from e

    @staticmethod
    def _to_clean_tool(tool: Tool) -> Tool:
        """Build a minimal FunctionTool that serializes cleanly.

        We extract only the wire-format fields (name, description, parameters,
        annotations) and construct a new FunctionTool with a dummy fn.
        The original ProxyTool stays in FastMCP's registry for actual execution.
        """
        from fastmcp.tools.function_tool import FunctionTool

        # Clean the parameters via JSON round-trip
        try:
            clean_params: dict[str, Any] = _safe_json_clone(tool.parameters)
        except (ValueError, RecursionError, TypeError):
            clean_params = {"type": "object", "properties": {}}

        # Clean annotations if present
        clean_annotations: dict[str, Any] | None
        try:
            clean_annotations = _safe_json_clone(
                tool.annotations.model_dump() if tool.annotations else None
            )
        except (ValueError, RecursionError, TypeError, AttributeError):
            clean_annotations = None

        # Build a fresh FunctionTool with no circular refs
        dummy_fn = lambda: None  # noqa: E731 -- never called, just for FunctionTool ctor
        new_tool = FunctionTool(
            fn=dummy_fn,
            name=str(tool.name) if tool.name else "unknown",
            description=str(tool.description) if tool.description else "",
            parameters=clean_params,
            annotations=mt.ToolAnnotations(**clean_annotations) if clean_annotations else None,
        )

        # Verify it serializes (exclude fn which is not serializable)
        try:
            new_tool.model_dump(
                by_alias=True, mode="json", exclude_none=True, exclude={"fn", "serializer"}
            )
        except Exception as e:
            # Last resort: strip parameters entirely
            logger.warning("Tool %s failed serialization, stripping params: %s", tool.name, e)
            new_tool = FunctionTool(
                fn=dummy_fn,
                name=str(tool.name) if tool.name else "unknown",
                description=str(tool.description) if tool.description else "",
                parameters={"type": "object", "properties": {}},
            )

        return new_tool


def _create_server_proxy(config: BridgeConfig, name: str, srv: ServerConfig) -> FastMCP:
    """Create a proxy for a single upstream MCP server.

    When a persistent connection is available (HTTP/SSE servers), the proxy
    uses the connection manager's factory which returns the *already-connected*
    client — avoiding a connect/disconnect cycle per tool call.

    When the server has auth configured but no persistent connection, we
    create a ``Client`` with ``auth=`` set so the proxy's upstream HTTP
    requests carry the right credentials.

    For servers without auth and without a persistent connection we fall
    back to the simpler dict-based ``create_proxy(config_dict)`` path.
    """
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


def _needs_oauth(srv: ServerConfig) -> bool:
    """Check if a server requires OAuth authentication."""
    if srv.auth == "oauth":
        return True
    if isinstance(srv.auth, dict) and "oauth" in srv.auth:
        return True
    return False


@overload
def create_bridge(
    config_path: str,
    *,
    oauth_cache_tokens: bool | None = ...,
    oauth_token_dir: str | None = ...,
    return_ss_manager: Literal[True],
) -> tuple[FastMCP, SharedServerManager]: ...


@overload
def create_bridge(
    config_path: str,
    *,
    oauth_cache_tokens: bool | None = ...,
    oauth_token_dir: str | None = ...,
    return_ss_manager: Literal[False] = ...,
) -> FastMCP: ...


def create_bridge(
    config_path: str,
    *,
    oauth_cache_tokens: bool | None = None,
    oauth_token_dir: str | None = None,
    return_ss_manager: bool = False,
) -> FastMCP | tuple[FastMCP, SharedServerManager]:
    """Create the bridge FastMCP server from a config file.

    Reads servers.json, creates a proxy for each enabled server,
    mounts them under namespaced prefixes, and adds meta-tools + health.

    Startup semantics for HTTP/OAuth servers:

    * Every enabled server is **mounted immediately** (proxy created).
    * HTTP/SSE servers are registered with the ``ConnectionManager``.
    * ``connect_all()`` opens persistent connections and **blocks** until
      every server has either connected or failed.  This guarantees that
      by the time the bridge serves its first request, no OAuth race
      conditions exist.
    * If an OAuth server fails authentication, it is marked
      ``_auth_failed`` and the factory raises ``AuthenticationError``
      (not retried by ``RetryMiddleware``).
    * The only way to retry is ``bridge__enable_server`` (manual toggle).

    CLI overrides (when provided) take precedence over the ``oauth`` section
    of the config file:

    - *oauth_cache_tokens*: ``False`` disables disk token caching globally.
    - *oauth_token_dir*: path override for the OAuth token directory.

    If *return_ss_manager* is True, returns a tuple of (bridge, ss_manager)
    so the caller can explicitly call stop_all() on shutdown.
    """
    global _bridge_config
    global _conn_manager

    config = BridgeConfig.load(config_path)
    _bridge_config = config  # Store for tool filtering

    # Apply CLI overrides on top of config-file oauth settings
    if oauth_cache_tokens is not None:
        config.oauth.cache_tokens = oauth_cache_tokens
    if oauth_token_dir is not None:
        config.oauth.token_dir = oauth_token_dir

    ss_manager = SharedServerManager(config)
    conn_manager = ConnectionManager(
        on_connected=lambda name: invalidate_tool_cache(),
    )
    _conn_manager = conn_manager

    @asynccontextmanager
    async def _lifespan(server: FastMCP) -> AsyncIterator[None]:
        await ss_manager.start_all()

        # Mount every enabled server.  OAuth servers are mounted even if
        # they don't have a cached token — the persistent connection attempt
        # in connect_all() (below) handles the single auth flow.  If it
        # fails, ConnectionManager marks _auth_failed and the factory
        # raises AuthenticationError for all subsequent calls.
        enabled = config.get_enabled_servers()
        for name, srv in enabled.items():
            # Pre-register HTTP/SSE servers for persistent connections.
            if conn_manager.is_http_server(srv):
                conn_manager.register(config, name, srv)

            try:
                proxy = _create_server_proxy(config, name, srv)
                server.mount(proxy, namespace=name)
                logger.info("Mounted server: %s (%s)", name, srv.transport.value)
            except Exception:
                logger.exception("Failed to mount server '%s'", name)

        # Open persistent connections to HTTP/SSE upstreams.
        # This BLOCKS until every server has connected or failed.
        # OAuth servers get exactly one auth attempt here.  If it fails
        # the connection is marked _auth_failed — no retry until manual
        # toggle via bridge__enable_server.
        await conn_manager.connect_all(config)
        logger.info("All connection attempts resolved — bridge is ready")

        try:
            yield
        finally:
            await conn_manager.close_all()
            await ss_manager.stop_all()

    bridge = FastMCP(
        name="mcp-bridge",
        instructions="MCP Bridge — proxies multiple MCP servers through a single endpoint.",
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
    from mcp_bridge.meta_tools import register_meta_tools

    register_meta_tools(bridge, config, conn_manager)

    # Health endpoint
    @bridge.custom_route("/health", methods=["GET"])
    async def health_check(request: Request) -> JSONResponse:
        server_statuses: dict[str, ServerStatusInfo] = {
            name: config.get_server_status(name) for name in config.servers
        }
        auth_failed = [n for n in conn_manager._connections if conn_manager.is_auth_failed(n)]
        response = HealthResponse(
            status="ok",
            servers=server_statuses,
            config_path=config.config_path,
            pending_oauth=auth_failed,
        )
        return JSONResponse(response.model_dump(mode="json"))

    if return_ss_manager:
        return bridge, ss_manager
    return bridge
