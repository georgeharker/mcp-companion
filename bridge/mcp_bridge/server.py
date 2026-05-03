"""FastMCP bridge server — proxies multiple MCP servers through one endpoint."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
import weakref
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any, ClassVar, Literal, overload

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

# Per-session server blocklist.
# Maps a session ID string to the set of server names disabled for that session.
# Entries are explicitly removed via the /sessions/{id}/filter DELETE endpoint
# or by the meta-tools.  The REST API also supports external management by
# session ID (used by the Neovim plugin for ACP session filtering).
_session_disabled: dict[str, set[str]] = {}

# Token registry: token -> bridge session_id.
# The Neovim plugin generates a UUID token per chat and embeds it as the MCP
# URL path (/mcp/<token>).  TokenRewriteMiddleware rewrites the path to /mcp
# and records token -> mcp-session-id from the FastMCP response header.
# GET /sessions/token/{token} lets the Lua side look up the bridge session_id.
_token_sessions: dict[str, str] = {}

# Pending token filters: token -> set of disabled server names.
# Stored when Lua POSTs a filter before the remote client has connected
# (i.e. before the token is mapped to a session_id).  Applied immediately
# by TokenRewriteMiddleware when the token is first seen.
_pending_token_filters: dict[str, set[str]] = {}

# Global schema normalization flag, set at server creation from
# ``--normalize-schema`` / ``MCP_BRIDGE_NORMALIZE_SCHEMA``.  When True, every
# tool emitted from ``tools/list`` is normalized at cache-fill time so strict
# providers (e.g. moonshot-ai/kimi) accept the resulting schemas.
_normalize_schemas_global: bool = False

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


async def _notify_session_by_id(session_id: str) -> None:
    """Send ``notifications/tools/list_changed`` to a specific session by ID."""
    for session in list(_active_sessions):
        try:
            sid = getattr(session, "_fastmcp_state_prefix", None) or str(id(session))
            if sid == session_id:
                await session.send_tool_list_changed()
                return
        except Exception:
            logger.debug("Failed to notify session %s", session_id, exc_info=True)


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


# Keywords that semantically belong with a specific "type" declaration.
# When we hoist a parent-level "type" into anyOf items, these travel with it.
_TYPE_SIBLING_KEYWORDS = frozenset((
    "items", "prefixItems", "minItems", "maxItems", "uniqueItems", "contains",
    "minLength", "maxLength", "pattern", "format",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "properties", "required", "additionalProperties", "patternProperties",
))


def _normalize_schema(schema: object) -> object:
    """Recursively fix schemas rejected by strict JSON Schema validators.

    Some providers (e.g. Moonshot-ai) reject schemas where ``type`` and
    ``anyOf`` coexist at the same level.  Pydantic generates this for
    ``Optional[list[str]]``::

        {"type": "array", "anyOf": [{"items": {...}}, {"type": "null"}]}

    The fix is to promote ``type`` (plus its sibling keywords such as
    ``items``) into each ``anyOf`` item that lacks its own ``type``::

        {"anyOf": [{"type": "array", "items": {...}}, {"type": "null"}]}
    """
    if isinstance(schema, list):
        return [_normalize_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema

    # Recurse into all values first so nested schemas are also clean.
    result: dict[str, Any] = {k: _normalize_schema(v) for k, v in schema.items()}

    if "type" not in result or "anyOf" not in result:
        return result

    # Pull the parent type and any keywords that travel with it.
    parent_type = result.pop("type")
    hoisted: dict[str, Any] = {"type": parent_type}
    for kw in _TYPE_SIBLING_KEYWORDS:
        if kw in result:
            hoisted[kw] = result.pop(kw)

    # Distribute into anyOf items that don't already declare a type.
    result["anyOf"] = [
        ({**hoisted, **item} if "type" not in item else item)
        for item in result["anyOf"]
    ]
    return result


def _normalize_tool_schema(tool: Tool) -> Tool:
    """Return a copy of *tool* with schema-normalized parameters.

    Assumes the tool parameters are already serializable (no circular refs).
    Only fixes schema compatibility issues (e.g. ``type`` + ``anyOf`` siblings).
    """
    from fastmcp.tools.function_tool import FunctionTool

    try:
        params = _normalize_schema(_safe_json_clone(tool.parameters))
        if not isinstance(params, dict):
            params = {"type": "object", "properties": {}}
    except (ValueError, RecursionError, TypeError):
        params = tool.parameters or {"type": "object", "properties": {}}

    dummy_fn = lambda: None  # noqa: E731
    return FunctionTool(
        fn=dummy_fn,
        name=str(tool.name) if tool.name else "unknown",
        description=str(tool.description) if tool.description else "",
        parameters=params,
        annotations=tool.annotations,
    )


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

    # Single-flight coalescing for concurrent cache misses.
    # When the cache is empty/stale and many sessions request tools/list at
    # once (e.g. after a tools_list_changed broadcast), only the first caller
    # issues the upstream fetch — every other caller awaits the same result.
    # Without this, N concurrent flows hit the same OAuth-backed Client and
    # race the SDK's auth-context lock.
    _inflight: ClassVar[asyncio.Future[list[Tool]] | None] = None
    _inflight_lock: ClassVar[asyncio.Lock] = asyncio.Lock()

    async def on_request(
        self,
        context: MiddlewareContext[mt.Request[Any, Any]],
        call_next: CallNext[mt.Request[Any, Any], Any],
    ) -> Any:
        """Track active sessions and notify session watches of new connections."""
        if context.fastmcp_context is not None:
            try:
                session = context.fastmcp_context.session
                sid = context.fastmcp_context.session_id
                is_new = session not in _active_sessions
                _active_sessions.add(session)

                if is_new:
                    try:
                        cp = getattr(session, "client_params", None)
                        ci = getattr(cp, "clientInfo", None) if cp else None
                        client_name = getattr(ci, "name", None) if ci else None
                        client_version = getattr(ci, "version", None) if ci else None
                        logger.info(
                            "New MCP session: id=%s client=%s version=%s",
                            sid,
                            client_name,
                            client_version,
                        )
                    except Exception:
                        logger.info("New MCP session: id=%s (no client info)", sid)

            except (RuntimeError, AttributeError):
                pass  # Session not yet established
        return await call_next(context)

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        now = time.time()
        cache_age = now - _tool_cache_time

        if _tool_cache is not None and cache_age < self.CACHE_TTL:
            logger.warning(
                "tools/list: CACHE HIT (%d tools, %.1fs old)",
                len(_tool_cache),
                cache_age,
            )
            return self._apply_session_filter(context, _tool_cache)

        tools = await self._fetch_or_join(context, call_next, cache_age)
        return self._apply_session_filter(context, tools)

    async def _fetch_or_join(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
        cache_age: float,
    ) -> list[Tool]:
        """Single-flight cache fill. First caller fetches; others await its result."""
        cls = type(self)

        async with cls._inflight_lock:
            fut = cls._inflight
            if fut is None or fut.done():
                fut = asyncio.get_running_loop().create_future()
                cls._inflight = fut
                is_owner = True
            else:
                is_owner = False

        if not is_owner:
            logger.debug("tools/list: joining in-flight fetch")
            return await fut

        logger.warning("tools/list: CACHE MISS - fetching fresh (cache_age=%.1fs)", cache_age)
        try:
            tools = await self._do_fetch(context, call_next)
            fut.set_result(tools)
            return tools
        except Exception as exc:
            fut.set_exception(exc)
            raise
        finally:
            async with cls._inflight_lock:
                if cls._inflight is fut:
                    cls._inflight = None

    async def _do_fetch(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> list[Tool]:
        """Fetch upstream, sanitize, filter, populate the global cache."""
        global _tool_cache, _tool_cache_time

        try:
            raw = list(await call_next(context))
        except Exception as e:
            logger.error("tools/list: upstream error, returning stale cache: %s", e)
            if _tool_cache is not None:
                return _tool_cache
            return []

        sanitized: list[Tool] = []
        for tool in raw:
            try:
                tool.model_dump(by_alias=True, mode="json", exclude_none=True)
                sanitized.append(tool)
            except (ValueError, RecursionError):
                logger.warning("Replacing circular tool: %s", tool.name)
                sanitized.append(self._to_clean_tool(tool))

        if _normalize_schemas_global:
            sanitized = [_normalize_tool_schema(t) for t in sanitized]
            logger.debug("tools/list: normalized %d tool schema(s)", len(sanitized))

        filtered = _filter_tools(sanitized)
        if len(filtered) < len(sanitized):
            logger.info(
                "tools/list: filtered %d -> %d tools based on tool_filter",
                len(sanitized),
                len(filtered),
            )

        _tool_cache = filtered
        _tool_cache_time = time.time()
        logger.info("tools/list: cached %d tools", len(filtered))
        return filtered

    @staticmethod
    def _apply_session_filter(
        context: MiddlewareContext[mt.ListToolsRequest],
        tools: list[Tool],
    ) -> list[Tool]:
        """Apply the per-session server blocklist."""
        if context.fastmcp_context is None:
            return tools
        try:
            sid = context.fastmcp_context.session_id
        except (RuntimeError, AttributeError):
            return tools

        blocked = _session_disabled.get(sid)
        if not blocked:
            return tools

        out: list[Tool] = [
            t
            for t in tools
            if _find_server_for_tool(str(t.name) if t.name else "")[0] not in blocked
        ]
        if len(out) < len(tools):
            logger.debug(
                "tools/list: session filter removed %d tool(s) for blocked servers %s",
                len(tools) - len(out),
                blocked,
            )
        return out

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
        # Per-session blocklist check: if the calling session has disabled
        # the server that owns this tool, reject immediately.
        if context.fastmcp_context is not None:
            try:
                sid = context.fastmcp_context.session_id
                blocked = _session_disabled.get(sid)
                if blocked:
                    sess_server, _ = _find_server_for_tool(str(tool_name))
                    if sess_server in blocked:
                        raise NotFoundError(
                            f"Tool '{tool_name}' is unavailable — server '{sess_server}' "
                            "is disabled for this session. Use bridge__session_enable_server "
                            "to re-enable it."
                        )
            except NotFoundError:
                raise
            except (RuntimeError, AttributeError):
                pass
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

        # Clean the parameters via JSON round-trip, then normalize the schema
        # so it is accepted by strict validators (e.g. Moonshot-ai rejects
        # schemas where "type" and "anyOf" coexist at the same level).
        try:
            clean_params = _normalize_schema(_safe_json_clone(tool.parameters))
            if not isinstance(clean_params, dict):
                clean_params = {"type": "object", "properties": {}}
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
    normalize_schemas: bool = ...,
    return_ss_manager: Literal[True],
) -> tuple[FastMCP, SharedServerManager]: ...


@overload
def create_bridge(
    config_path: str,
    *,
    oauth_cache_tokens: bool | None = ...,
    oauth_token_dir: str | None = ...,
    normalize_schemas: bool = ...,
    return_ss_manager: Literal[False] = ...,
) -> FastMCP: ...


def create_bridge(
    config_path: str,
    *,
    oauth_cache_tokens: bool | None = None,
    oauth_token_dir: str | None = None,
    normalize_schemas: bool = False,
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
    global _normalize_schemas_global

    config = BridgeConfig.load(config_path)
    _bridge_config = config  # Store for tool filtering
    _normalize_schemas_global = normalize_schemas
    if normalize_schemas:
        logger.info("Schema normalization enabled globally for tools/list")

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

        # Start persistent connections to HTTP/SSE upstreams in the background.
        # OAuth servers get exactly one auth attempt here.  The bridge starts
        # serving immediately; servers requiring OAuth are available once the
        # user completes the browser flow.  If auth fails the connection is
        # marked _auth_failed — no retry until manual toggle via bridge__enable_server.
        await conn_manager.connect_all(config)
        logger.info("Connection tasks started — bridge is ready")

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

    # --- Session management REST API ---
    # These endpoints allow external clients (e.g. the Neovim plugin) to
    # list active MCP sessions and manage per-session server filters by
    # session ID, without needing to be the session owner.

    @bridge.custom_route("/sessions", methods=["GET"])
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

    @bridge.custom_route("/sessions/{session_id}/filter", methods=["GET", "POST", "DELETE"])
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
            # Disable all servers not in allowed list (except _bridge meta-server)
            disabled_list = [s for s in config.servers if s not in allowed_set and s != "_bridge"]
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

    @bridge.custom_route("/sessions/token/{token}", methods=["GET"])
    async def lookup_session_token(request: Request) -> JSONResponse:
        """Look up the bridge session_id associated with a token.

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

    @bridge.custom_route("/sessions/token/{token}/filter", methods=["GET", "POST", "DELETE"])
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
                return JSONResponse({"token": token, "session_id": session_id,
                                     "disabled_servers": sorted(disabled)})
            pending = _pending_token_filters.get(token, set())
            return JSONResponse({"token": token, "session_id": None,
                                 "pending": True, "disabled_servers": sorted(pending)})

        if request.method == "DELETE":
            _pending_token_filters.pop(token, None)
            if session_id:
                removed = _session_disabled.pop(session_id, None)
                await _notify_session_by_id(session_id)
                return JSONResponse({"token": token, "session_id": session_id,
                                     "action": "cleared",
                                     "previously_disabled": sorted(removed) if removed else []})
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
                d = {s for s in config.servers if s not in allowed_set and s != "_bridge"}
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
            logger.info("REST token filter: token=%s session=%s disabled=%s",
                        token, session_id,
                        sorted(_session_disabled.get(session_id, set())))
            return JSONResponse({"token": token, "session_id": session_id,
                                 "disabled_servers": sorted(_session_disabled.get(session_id, set()))})

        # Session not yet connected — store as pending
        current = set(_pending_token_filters.get(token, set()))
        new_disabled = _resolve_disabled(current)
        if new_disabled:
            _pending_token_filters[token] = new_disabled
        else:
            _pending_token_filters.pop(token, None)
        logger.info("REST token filter (pending): token=%s disabled=%s",
                    token, sorted(new_disabled) if new_disabled else [])
        return JSONResponse({"token": token, "session_id": None, "pending": True,
                             "disabled_servers": sorted(new_disabled) if new_disabled else []})

    if return_ss_manager:
        return bridge, ss_manager
    return bridge
