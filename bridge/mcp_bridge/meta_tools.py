"""Meta-tools for the bridge — status, enable/disable servers."""

from __future__ import annotations

import logging

from fastmcp import Context, FastMCP

from mcp_bridge.config import BridgeConfig, ServerStatusInfo
from mcp_bridge.connections import ConnectionManager

logger = logging.getLogger("mcp-bridge")


def register_meta_tools(
    bridge: FastMCP, config: BridgeConfig, conn_manager: ConnectionManager
) -> None:
    """Register bridge management tools on the FastMCP server."""

    @bridge.tool()
    def bridge__status() -> dict[str, ServerStatusInfo]:
        """Get status of all configured MCP servers.

        Returns a dict of server names to their configuration and status.
        """
        return {name: config.get_server_status(name) for name in config.servers}

    @bridge.tool()
    async def bridge__enable_server(server_name: str) -> str:
        """Enable a disabled MCP server and mount it on the bridge.

        This is also the manual retry path for servers that failed
        authentication at startup.  It resets any auth-failure flag
        and attempts a fresh connection.

        Args:
            server_name: Name of the server to enable.

        Returns:
            Status message.
        """
        if server_name not in config.servers:
            return f"Error: Server '{server_name}' not found"
        srv = config.servers[server_name]

        # Allow re-enable even if not disabled (manual retry for auth-failed).
        srv.disabled = False

        # Reset auth-failure so ConnectionManager will attempt reconnect.
        if conn_manager.is_auth_failed(server_name):
            conn_manager.reset_auth_failure(server_name)

        try:
            from mcp_bridge.server import (
                _create_server_proxy,
                invalidate_tool_cache,
            )

            # Open persistent connection for HTTP/SSE servers
            if conn_manager.is_http_server(srv):
                if conn_manager.has_connection(server_name):
                    # Already registered — just reconnect
                    await conn_manager.connect(config, server_name, srv)
                else:
                    conn_manager.register(config, server_name, srv)
                    await conn_manager.connect(config, server_name, srv)

            proxy = _create_server_proxy(config, server_name, srv)
            bridge.mount(proxy, namespace=server_name)
            invalidate_tool_cache()
            logger.info("Dynamically mounted server: %s", server_name)
            return f"Server '{server_name}' enabled and mounted"
        except Exception as e:
            logger.exception("Failed to mount server '%s' on enable", server_name)
            return f"Server '{server_name}' enabled but failed to mount: {e}"

    @bridge.tool()
    async def bridge__disable_server(server_name: str) -> str:
        """Disable an MCP server and unmount it from the bridge.

        Args:
            server_name: Name of the server to disable.

        Returns:
            Status message.
        """
        if server_name not in config.servers:
            return f"Error: Server '{server_name}' not found"
        srv = config.servers[server_name]
        if srv.disabled:
            return f"Server '{server_name}' is already disabled"

        srv.disabled = True

        # Close persistent connection first (before removing providers)
        if conn_manager.has_connection(server_name):
            await conn_manager.disconnect(server_name)

        # Remove all providers whose namespace matches server_name.
        # AggregateProvider wraps namespaced providers via wrap_transform(Namespace(...)).
        # The wrapped provider's repr contains the namespace string, so we inspect it.
        # We also match by checking the provider's _namespace attribute if it exists
        # (set by some FastMCP wrapper types).
        try:
            from mcp_bridge.server import invalidate_tool_cache

            before = len(bridge.providers)

            def _provider_matches(p: object) -> bool:
                """Return True if provider belongs to server_name's namespace."""
                # FastMCP wraps with Namespace transform — check repr for namespace tag
                r = repr(p)
                # NamespaceTransform repr includes the namespace string
                if f"namespace='{server_name}'" in r or f'namespace="{server_name}"' in r:
                    return True
                # Fallback: check _namespace attribute
                if getattr(p, "_namespace", None) == server_name:
                    return True
                return False

            bridge.providers = [p for p in bridge.providers if not _provider_matches(p)]
            removed = before - len(bridge.providers)

            invalidate_tool_cache()
            logger.info("Removed %d provider(s) for server '%s'", removed, server_name)

            if removed > 0:
                return (
                    f"Server '{server_name}' disabled and unmounted ({removed} provider(s) removed)"
                )
            else:
                return (
                    f"Server '{server_name}' disabled (no active providers found to remove — "
                    "it may not have been mounted)"
                )
        except Exception as e:
            logger.exception("Failed to unmount server '%s' on disable", server_name)
            return f"Server '{server_name}' disabled but failed to unmount: {e}"

    @bridge.tool()
    async def bridge__session_disable_server(
        server_name: str, ctx: Context, chat_id: str | None = None
    ) -> str:
        """Disable an MCP server for the current session only.

        Unlike bridge__disable_server (which affects all sessions globally),
        this only hides the server's tools from the calling MCP client session.
        Other sessions are unaffected.  The change is automatically reverted
        when the session ends.

        Args:
            server_name: Name of the server to disable for this session.
            chat_id: Optional chat identifier for per-chat filtering when multiple
                     chats share a single MCP connection (e.g., HTTP adapter).

        Returns:
            JSON with session_id/chat_id and updated disabled_servers list.
        """
        if server_name not in config.servers:
            return f"Error: Server '{server_name}' not found"

        from mcp_bridge.server import _session_disabled

        # Use chat_id if provided, otherwise fall back to MCP session_id
        sid = chat_id if chat_id else ctx.session_id
        if sid not in _session_disabled:
            _session_disabled[sid] = set()
        _session_disabled[sid].add(server_name)

        # Notify this session only so its tool list refreshes immediately.
        # (Only effective when using ctx.session_id, not chat_id)
        if not chat_id:
            try:
                await ctx.session.send_tool_list_changed()
            except Exception:
                logger.debug("Failed to notify session of tool list change", exc_info=True)

        logger.info("Session %s: disabled server '%s'", sid, server_name)

        import json
        return json.dumps({
            "session_id": sid,
            "action": "disabled",
            "server": server_name,
            "disabled_servers": sorted(_session_disabled.get(sid, set())),
        })

    @bridge.tool()
    async def bridge__session_enable_server(
        server_name: str, ctx: Context, chat_id: str | None = None
    ) -> str:
        """Re-enable an MCP server that was disabled for the current session.

        Reverses the effect of bridge__session_disable_server for the
        calling session.  Has no effect if the server was not session-disabled.

        Args:
            server_name: Name of the server to re-enable for this session.
            chat_id: Optional chat identifier for per-chat filtering when multiple
                     chats share a single MCP connection (e.g., HTTP adapter).

        Returns:
            JSON with session_id/chat_id and updated disabled_servers list.
        """
        if server_name not in config.servers:
            return f"Error: Server '{server_name}' not found"

        from mcp_bridge.server import _session_disabled

        # Use chat_id if provided, otherwise fall back to MCP session_id
        sid = chat_id if chat_id else ctx.session_id
        blocked = _session_disabled.get(sid)
        if not blocked or server_name not in blocked:
            import json
            return json.dumps({
                "session_id": sid,
                "action": "no_change",
                "server": server_name,
                "message": f"Server '{server_name}' is not session-disabled",
                "disabled_servers": sorted(_session_disabled.get(sid, set())),
            })

        blocked.discard(server_name)
        # Clean up empty sets
        if not blocked:
            _session_disabled.pop(sid, None)

        # Notify this session only so its tool list refreshes immediately.
        if not chat_id:
            try:
                await ctx.session.send_tool_list_changed()
            except Exception:
                logger.debug("Failed to notify session of tool list change", exc_info=True)

        logger.info("Session %s: re-enabled server '%s'", sid, server_name)

        import json
        return json.dumps({
            "session_id": sid,
            "action": "enabled",
            "server": server_name,
            "disabled_servers": sorted(_session_disabled.get(sid, set())),
        })

    @bridge.tool()
    async def bridge__session_status(ctx: Context, chat_id: str | None = None) -> str:
        """Get the session-disabled server list for the current MCP session.

        Args:
            chat_id: Optional chat identifier for per-chat filtering when multiple
                     chats share a single MCP connection (e.g., HTTP adapter).

        Returns a JSON object with:
          - session_id: the MCP session identifier (or chat_id if provided)
          - disabled_servers: list of server names disabled for this session

        Returns:
            JSON string with session status.
        """
        from mcp_bridge.server import _session_disabled

        sid = chat_id if chat_id else ctx.session_id
        blocked = list(_session_disabled.get(sid, set()))
        blocked.sort()

        import json
        return json.dumps({
            "session_id": sid,
            "disabled_servers": blocked,
        })
