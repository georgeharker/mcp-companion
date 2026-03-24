"""FastMCP bridge server — proxies multiple MCP servers through one endpoint."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import httpx
import mcp.types as mt
from fastmcp import Client, FastMCP
from fastmcp.server import create_proxy
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import Tool
from fastmcp.tools.tool import ToolResult
from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp_bridge.auth import (
    build_auth,
    clear_oauth_cache,
    has_valid_oauth_token,
    is_stale_client_error,
)
from mcp_bridge.config import (
    BridgeConfig,
    HealthResponse,
    ServerConfig,
    ServerStatusInfo,
    _interpolate_str,  # noqa: PLC2701
)
from mcp_bridge.sharedserver import SharedServerManager

logger = logging.getLogger("mcp-bridge")

# Track failed servers to avoid repeated errors
_failed_servers: dict[str, str] = {}  # server_name -> error message

# Timeout for individual upstream server queries during tools/list
UPSTREAM_TOOL_LIST_TIMEOUT = 5.0  # seconds


# Global tool cache - shared across middleware instances
_tool_cache: list[Tool] | None = None
_tool_cache_time: float = 0

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
    """Invalidate the tool cache, forcing a refresh on next tools/list."""
    global _tool_cache, _tool_cache_time
    _tool_cache = None
    _tool_cache_time = 0
    logger.info("Tool cache invalidated")


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

        If a tool call fails due to upstream errors (connection, auth, etc.),
        return an error message instead of crashing. For OAuth-related errors,
        clear the cache so the next attempt triggers fresh authentication.
        """
        # context.message is CallToolRequestParams, which has .name directly
        tool_name = context.message.name if context.message else "unknown"
        try:
            return await call_next(context)
        except Exception as e:
            # Extract server name from namespaced tool name (e.g., "github__list_repos" -> "github")
            server_name = tool_name.split("__")[0] if "__" in tool_name else None

            # Check if this is a stale OAuth error
            if server_name and is_stale_client_error(e):
                logger.warning(
                    "Tool '%s' failed with stale OAuth error, clearing cache for server '%s': %s",
                    tool_name,
                    server_name,
                    e,
                )
                # Clear OAuth cache so next attempt re-authenticates
                from mcp_bridge.config import OAuthConfig

                token_dir = OAuthConfig().token_dir_path
                clear_oauth_cache(server_name, token_dir)
                _failed_servers[server_name] = f"OAuth error: {e}"

            logger.error("Tool '%s' failed: %s", tool_name, e)

            # Return error as tool result instead of crashing
            error_msg = f"Error calling tool '{tool_name}': {e}"
            return ToolResult(content=[mt.TextContent(type="text", text=error_msg)])

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

    When the server has auth configured, we create a ``Client`` with
    ``auth=`` set so the proxy's upstream HTTP requests carry the right
    credentials.  For servers without auth we fall back to the simpler
    dict-based ``create_proxy(config_dict)`` path.
    """
    auth: httpx.Auth | None = build_auth(
        name,
        auth_config=srv.auth,
        server_url=srv.url,
        token_dir=config.oauth.token_dir_path,
        cache_tokens=config.oauth.cache_tokens,
    )

    if auth is not None and srv.url:
        # Auth requires a Client so we can inject httpx.Auth into the transport
        client = Client(_interpolate_str(srv.url), auth=auth)
        return create_proxy(client, name=name)

    # No auth — use the standard config-dict path
    proxy_config = config.to_fastmcp_config(name)
    return create_proxy(proxy_config.model_dump(exclude_none=True), name=name)


def _needs_oauth(srv: ServerConfig) -> bool:
    """Check if a server requires OAuth authentication."""
    if srv.auth == "oauth":
        return True
    if isinstance(srv.auth, dict) and srv.auth.get("type") == "oauth":
        return True
    return False


# Track pending OAuth servers for background auth
_pending_oauth_servers: dict[str, tuple[BridgeConfig, str, ServerConfig]] = {}
_oauth_task: asyncio.Task[None] | None = None


async def _background_oauth_auth(bridge: FastMCP) -> None:
    """Background task to authenticate OAuth servers and mount them when ready."""
    global _pending_oauth_servers

    while _pending_oauth_servers:
        # Process one server at a time to avoid concurrent OAuth flows
        name, (config, name, srv) = next(iter(_pending_oauth_servers.items()))

        logger.info("Starting background OAuth for server: %s", name)
        try:
            # Build auth - this creates the OAuth provider
            auth = build_auth(
                name,
                auth_config=srv.auth,
                server_url=srv.url,
                token_dir=config.oauth.token_dir_path,
                cache_tokens=config.oauth.cache_tokens,
            )

            if auth is None:
                logger.warning("No auth built for OAuth server '%s'", name)
                del _pending_oauth_servers[name]
                continue

            # Create client with OAuth auth and connect to trigger the flow
            # This will open browser for user authentication
            url = _interpolate_str(srv.url) if srv.url else None
            if not url:
                logger.warning("No URL for OAuth server '%s'", name)
                del _pending_oauth_servers[name]
                continue

            async with Client(url, auth=auth) as client:
                # List tools to ensure we're fully authenticated
                await client.list_tools()

            # If we get here, auth succeeded - create and mount the proxy
            proxy = _create_server_proxy(config, name, srv)
            bridge.mount(proxy, namespace=name)
            logger.info("Background OAuth succeeded, mounted server: %s", name)

            # Invalidate tool cache so next tools/list includes this server
            invalidate_tool_cache()

            # Remove from pending
            del _pending_oauth_servers[name]

            # TODO: Send ToolListChangedNotification to connected clients
            # This requires access to active sessions which FastMCP doesn't expose easily

        except Exception as e:
            logger.warning("Background OAuth failed for server '%s': %s", name, e)
            # Remove from pending - will retry on next bridge restart
            del _pending_oauth_servers[name]

        # Small delay between servers
        await asyncio.sleep(1)


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

    OAuth servers without cached tokens are skipped at startup and queued
    for background authentication. When auth completes, they are mounted
    and the tool cache is invalidated.

    CLI overrides (when provided) take precedence over the ``oauth`` section
    of the config file:

    - *oauth_cache_tokens*: ``False`` disables disk token caching globally.
    - *oauth_token_dir*: path override for the OAuth token directory.

    If *return_ss_manager* is True, returns a tuple of (bridge, ss_manager)
    so the caller can explicitly call stop_all() on shutdown.
    """
    global _pending_oauth_servers, _oauth_task, _bridge_config

    config = BridgeConfig.load(config_path)
    _bridge_config = config  # Store for tool filtering

    # Apply CLI overrides on top of config-file oauth settings
    if oauth_cache_tokens is not None:
        config.oauth.cache_tokens = oauth_cache_tokens
    if oauth_token_dir is not None:
        config.oauth.token_dir = oauth_token_dir

    ss_manager = SharedServerManager(config)
    _pending_oauth_servers = {}

    @asynccontextmanager
    async def _lifespan(server: FastMCP) -> AsyncIterator[None]:
        global _oauth_task
        await ss_manager.start_all()

        # Start background OAuth task if there are pending servers
        if _pending_oauth_servers:
            logger.info(
                "Starting background OAuth for %d servers: %s",
                len(_pending_oauth_servers),
                list(_pending_oauth_servers.keys()),
            )
            _oauth_task = asyncio.create_task(_background_oauth_auth(server))

        try:
            yield
        finally:
            if _oauth_task and not _oauth_task.done():
                _oauth_task.cancel()
                try:
                    await _oauth_task
                except asyncio.CancelledError:
                    pass
            await ss_manager.stop_all()

    bridge = FastMCP(
        name="mcp-bridge",
        instructions="MCP Bridge — proxies multiple MCP servers through a single endpoint.",
        dereference_schemas=False,  # Disabled: circular $ref causes infinite recursion
        middleware=[ToolProcessingMiddleware()],  # Caching, filtering, sanitization, error handling
        lifespan=_lifespan,
    )

    # Mount each enabled server as a namespaced proxy
    enabled = config.get_enabled_servers()
    for name, srv in enabled.items():
        # Check if OAuth server needs authentication
        if _needs_oauth(srv):
            token_dir = config.oauth.token_dir_path
            if not has_valid_oauth_token(name, token_dir):
                logger.info(
                    "OAuth server '%s' has no cached token - deferring to background auth",
                    name,
                )
                _pending_oauth_servers[name] = (config, name, srv)
                continue

        try:
            proxy = _create_server_proxy(config, name, srv)
            bridge.mount(proxy, namespace=name)
            logger.info("Mounted server: %s (%s)", name, srv.transport.value)
        except Exception:
            logger.exception("Failed to mount server '%s'", name)

    # Register meta-tools
    from mcp_bridge.meta_tools import register_meta_tools

    register_meta_tools(bridge, config)

    # Health endpoint
    @bridge.custom_route("/health", methods=["GET"])
    async def health_check(request: Request) -> JSONResponse:
        server_statuses: dict[str, ServerStatusInfo] = {
            name: config.get_server_status(name) for name in config.servers
        }
        response = HealthResponse(
            status="ok",
            servers=server_statuses,
            config_path=config.config_path,
            pending_oauth=list(_pending_oauth_servers.keys()),
        )
        return JSONResponse(response.model_dump(mode="json"))

    if return_ss_manager:
        return bridge, ss_manager
    return bridge
