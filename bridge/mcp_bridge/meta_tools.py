"""Meta-tools for the bridge — status, enable/disable servers."""

from __future__ import annotations

import logging

from fastmcp import FastMCP

from mcp_bridge.config import BridgeConfig, ServerStatusInfo

logger = logging.getLogger("mcp-bridge")


def register_meta_tools(bridge: FastMCP, config: BridgeConfig) -> None:
    """Register bridge management tools on the FastMCP server."""

    @bridge.tool()
    def bridge__status() -> dict[str, ServerStatusInfo]:
        """Get status of all configured MCP servers.

        Returns a dict of server names to their configuration and status.
        """
        return {name: config.get_server_status(name) for name in config.servers}

    @bridge.tool()
    def bridge__enable_server(server_name: str) -> str:
        """Enable a disabled MCP server and mount it on the bridge.

        Args:
            server_name: Name of the server to enable.

        Returns:
            Status message.
        """
        if server_name not in config.servers:
            return f"Error: Server '{server_name}' not found"
        srv = config.servers[server_name]
        if not srv.disabled:
            return f"Server '{server_name}' is already enabled"

        srv.disabled = False

        # Try to dynamically mount the proxy
        try:
            from mcp_bridge.auth import has_valid_oauth_token
            from mcp_bridge.server import (
                _needs_oauth,
                _create_server_proxy,
                invalidate_tool_cache,
            )

            if _needs_oauth(srv):
                token_dir = config.oauth.token_dir_path
                if not has_valid_oauth_token(server_name, token_dir):
                    return (
                        f"Server '{server_name}' enabled but requires OAuth authentication. "
                        "Background auth will be attempted."
                    )

            proxy = _create_server_proxy(config, server_name, srv)
            bridge.mount(proxy, namespace=server_name)
            invalidate_tool_cache()
            logger.info("Dynamically mounted server: %s", server_name)
            return f"Server '{server_name}' enabled and mounted"
        except Exception as e:
            logger.exception("Failed to mount server '%s' on enable", server_name)
            return f"Server '{server_name}' enabled but failed to mount: {e}"

    @bridge.tool()
    def bridge__disable_server(server_name: str) -> str:
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

        # Remove all providers whose namespace matches server_name.
        # AggregateProvider wraps namespaced providers via wrap_transform(Namespace(...)).
        # The wrapped provider's repr contains the namespace string, so we inspect it.
        # We also match by checking the provider's _namespace attribute if it exists
        # (set by some FastMCP wrapper types).
        try:
            from mcp_bridge.server import invalidate_tool_cache

            before = len(bridge.providers)
            namespace_prefix = f"{server_name}_"

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
