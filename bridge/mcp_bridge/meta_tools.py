"""Meta-tools for the bridge — status, enable/disable servers."""

from __future__ import annotations

from fastmcp import FastMCP

from mcp_bridge.config import BridgeConfig, ServerStatusInfo


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
        """Enable a disabled MCP server.

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
        # Note: The proxy for this server needs to be mounted.
        # For now, this only updates the in-memory config.
        # A full implementation would dynamically mount/unmount.
        return f"Server '{server_name}' enabled (restart bridge for full effect)"

    @bridge.tool()
    def bridge__disable_server(server_name: str) -> str:
        """Disable an MCP server.

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
        return f"Server '{server_name}' disabled (restart bridge for full effect)"
