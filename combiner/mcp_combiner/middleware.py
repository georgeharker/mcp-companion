"""ToolProcessingMiddleware — the combiner's request-path middleware.

Moved verbatim from server.py during the decomposition. Handles: active
session tracking (on_request), the cached/single-flight tools/list pipeline
with per-session filtering and the schema egress pass (on_list_tools), and
tool-call routing/error strategy incl. neovim virtual tools and lazy upstream
liveness bookkeeping (on_call_tool).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from typing import Any, ClassVar

import mcp.types as mt
from fastmcp.exceptions import NotFoundError, ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import Tool
from fastmcp.tools.tool import ToolResult

from mcp_combiner import nvim_proxy
from mcp_combiner.auth import clear_oauth_cache, is_stale_client_error
from mcp_combiner.connections import AuthenticationError
from mcp_combiner.runtime import RUNTIME
from mcp_combiner.schemafix import _finalize_schemas, _sanitize_tools
from mcp_combiner.toolcache import (
    _filter_tools,
    _find_server_for_tool,
    _is_transport_dead,
    _merge_stale_server_tools,
)

logger = logging.getLogger("mcp-combiner")


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
                is_new = RUNTIME.sessions.track(session)

                # Build the session_id -> token reverse map used to route
                # neovim_* calls back to the editor that owns this chat.
                nvim_proxy.record_session_token(sid)

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
        cache_age = now - RUNTIME.tools.cache_time

        if RUNTIME.tools.cache is not None and cache_age < self.CACHE_TTL:
            logger.warning(
                "tools/list: CACHE HIT (%d tools, %.1fs old)",
                len(RUNTIME.tools.cache),
                cache_age,
            )
            base = self._apply_session_filter(context, RUNTIME.tools.cache)
            full = await nvim_proxy.append_nvim_tools(context, base, RUNTIME.sessions.disabled)
            return _finalize_schemas(full)

        tools = await self._fetch_or_join(context, call_next, cache_age)
        base = self._apply_session_filter(context, tools)
        full = await nvim_proxy.append_nvim_tools(context, base, RUNTIME.sessions.disabled)
        return _finalize_schemas(full)

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

        if RUNTIME.tools.cache is None:
            # No prior cache (first fetch, or just invalidated). cache_age would
            # be a meaningless now-minus-epoch value here, so don't log it.
            logger.warning("tools/list: CACHE MISS - fetching fresh (no prior cache)")
        else:
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

        try:
            raw = list(await call_next(context))
        except Exception as e:
            logger.error("tools/list: upstream error, returning stale cache: %s", e)
            if RUNTIME.tools.cache is not None:
                return RUNTIME.tools.cache
            return []

        sanitized = _sanitize_tools(raw)

        # NB: schema_fixes + object-shape coercion are NOT applied here. They run
        # once at the on_list_tools egress (_finalize_schemas) over the COMPLETE
        # assembled list — upstream AND the appended neovim virtual tools — so no
        # tool source can bypass them. Only the circular-$ref rebuild (above) is
        # fetch-local, because it must guard what enters the cache.
        filtered = _filter_tools(sanitized)
        if len(filtered) < len(sanitized):
            logger.info(
                "tools/list: filtered %d -> %d tools based on tool_filter",
                len(sanitized),
                len(filtered),
            )

        # Re-inject last-known-good tools for servers that are only transiently
        # absent (mid-reconnect), so a peer server's reconnect can't blank them.
        # The reinjected slices were already sanitized + filtered when cached, so
        # they are appended after _filter_tools rather than run through it again.
        merged = _merge_stale_server_tools(filtered, time.time())

        RUNTIME.tools.set_cache(merged, time.time())
        logger.info("tools/list: cached %d tools", len(merged))
        return merged

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

        blocked = RUNTIME.sessions.disabled_for(sid)
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

        # Virtual native server: intercept `neovim_*` and route over the
        # back-channel instead of the upstream proxy. These tools are never in
        # FastMCP's registry, so we must handle them before call_next.
        if nvim_proxy.is_nvim_tool(tool_name):
            return await nvim_proxy.call_nvim_tool(context, tool_name, RUNTIME.sessions.disabled)

        # Per-session blocklist check: if the calling session has disabled
        # the server that owns this tool, reject immediately.
        if context.fastmcp_context is not None:
            try:
                sid = context.fastmcp_context.session_id
                blocked = RUNTIME.sessions.disabled_for(sid)
                if blocked:
                    sess_server, _ = _find_server_for_tool(str(tool_name))
                    if sess_server in blocked:
                        raise NotFoundError(
                            f"Tool '{tool_name}' is unavailable — server '{sess_server}' "
                            "is disabled for this session. Use combiner__session_enable_server "
                            "to re-enable it."
                        )
            except NotFoundError:
                raise
            except (RuntimeError, AttributeError):
                pass
        # Resolve the owning server once (longest prefix wins — disambiguates
        # names that are prefixes of each other) for failure/recovery bookkeeping
        # around the call result.
        call_server: str | None = None
        if RUNTIME.config:
            for sname in sorted(RUNTIME.config.servers, key=len, reverse=True):
                if tool_name.startswith(sname + "_"):
                    call_server = sname
                    break

        try:
            result = await call_next(context)
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
                f"failed. Use combiner__enable_server to retry authentication."
            ) from e
        except Exception as e:
            server_name = call_server
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

            # Lazy liveness: a transport/process death (crashed stdio subprocess,
            # broken pipe, dropped connection) means the upstream isn't serving.
            # Record it (RUNTIME.tools.failed_servers) so /health and combiner__status show
            # the server as down — for stdio there is no connection lifecycle, so
            # this mark is the ONLY down signal; for HTTP we also downgrade out of
            # "ready". 429s are transient (handled above) and skipped here. The
            # mark is cleared on the next successful call (see the else branch).
            if server_name and (_is_transport_dead(e) or is_stale_client_error(e)):
                RUNTIME.tools.record_failure(server_name, f"{type(e).__name__}: {e}")
                if RUNTIME.conn_manager is not None:
                    RUNTIME.conn_manager.mark_tools_unready(server_name)

            # Check if this is a stale OAuth error — clear cache so next
            # attempt triggers fresh authentication
            if server_name and is_stale_client_error(e):
                logger.warning(
                    "Tool '%s' failed with stale OAuth error, clearing cache for '%s': %s",
                    tool_name,
                    server_name,
                    e,
                )
                from mcp_combiner.config import OAuthConfig

                token_dir = OAuthConfig().token_dir_path
                clear_oauth_cache(server_name, token_dir)
                RUNTIME.tools.record_failure(server_name, f"OAuth error: {e}")

            logger.error("Tool '%s' failed: %s", tool_name, e)
            raise ToolError(f"Error calling tool '{tool_name}': {e}") from e
        else:
            # Success proves the server is alive again — clear any stale failure
            # mark so /health and combiner__status flip it back to ready. For a
            # crashed stdio server this is the recovery signal (a fresh subprocess
            # answered); for HTTP the reconnect monitor already restored it.
            if call_server and RUNTIME.tools.clear_failure(call_server):
                logger.info("Server '%s' recovered on a successful call", call_server)
            return result
