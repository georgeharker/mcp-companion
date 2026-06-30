"""Meta-tools for the combiner — status, enable/disable servers."""

from __future__ import annotations

import logging

from fastmcp import Context, FastMCP

from mcp_combiner.config import CombinerConfig, ServerConfig, ServerStatusInfo
from mcp_combiner.connections import ConnectionManager
from mcp_combiner.sharedserver import SharedServerManager

logger = logging.getLogger("mcp-combiner")


def register_meta_tools(
    combiner: FastMCP,
    config: CombinerConfig,
    conn_manager: ConnectionManager,
    ss_manager: SharedServerManager,
) -> None:
    """Register combiner management tools on the FastMCP server."""

    @combiner.tool()
    def combiner__status() -> dict[str, ServerStatusInfo]:
        """Get status of all configured MCP servers.

        Returns a dict of server names to their configuration and status.
        """
        return {name: config.get_server_status(name) for name in config.servers}

    @combiner.tool()
    async def combiner__enable_server(server_name: str) -> str:
        """Enable a disabled MCP server and mount it on the combiner.

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
            from mcp_combiner.server import (
                _create_server_proxy,
                invalidate_tool_cache,
            )

            # Start the backing sharedserver first (use + health-poll), so the
            # connection below has something to reach. No-op for non-sharedserver
            # servers or ones already up. "Enabled" is the start trigger.
            await ss_manager.ensure_started(server_name)

            # Open persistent connection for HTTP/SSE servers
            if conn_manager.is_http_server(srv):
                if conn_manager.has_connection(server_name):
                    # Already registered — just reconnect
                    await conn_manager.connect(config, server_name, srv)
                else:
                    conn_manager.register(config, server_name, srv)
                    await conn_manager.connect(config, server_name, srv)

            proxy = _create_server_proxy(config, server_name, srv)
            combiner.mount(proxy, namespace=server_name)
            invalidate_tool_cache()
            logger.info("Dynamically mounted server: %s", server_name)
            return f"Server '{server_name}' enabled and mounted"
        except Exception as e:
            logger.exception("Failed to mount server '%s' on enable", server_name)
            return f"Server '{server_name}' enabled but failed to mount: {e}"

    @combiner.tool()
    async def combiner__disable_server(server_name: str) -> str:
        """Disable an MCP server and unmount it from the combiner.

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

        # Drop our reference on the backing sharedserver (unuse). It stops after
        # its grace period if no other client still references it. No-op for
        # non-sharedserver servers.
        await ss_manager.ensure_stopped(server_name)

        # Remove all providers whose namespace matches server_name.
        # AggregateProvider wraps namespaced providers via wrap_transform(Namespace(...)).
        # The wrapped provider's repr contains the namespace string, so we inspect it.
        # We also match by checking the provider's _namespace attribute if it exists
        # (set by some FastMCP wrapper types).
        try:
            from mcp_combiner.server import invalidate_tool_cache

            before = len(combiner.providers)

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

            combiner.providers = [p for p in combiner.providers if not _provider_matches(p)]
            removed = before - len(combiner.providers)

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

    async def _unmount_server(server_name: str) -> None:
        """Tear down a server's connection, backing process, and providers.

        Mirrors combiner__disable_server's teardown but does not touch the
        ``disabled`` flag — the caller decides whether the server stays gone
        (removed) or is re-mounted with a fresh definition (changed).
        """
        if conn_manager.has_connection(server_name):
            await conn_manager.disconnect(server_name)
        await ss_manager.ensure_stopped(server_name)

        def _provider_matches(p: object) -> bool:
            r = repr(p)
            if f"namespace='{server_name}'" in r or f'namespace="{server_name}"' in r:
                return True
            return getattr(p, "_namespace", None) == server_name

        combiner.providers = [p for p in combiner.providers if not _provider_matches(p)]

    async def _mount_server(server_name: str, srv: ServerConfig) -> None:
        """Start, connect, and mount a server. Mirrors combiner__enable_server."""
        from mcp_combiner.server import _create_server_proxy

        await ss_manager.ensure_started(server_name)
        if conn_manager.is_http_server(srv):
            if conn_manager.is_auth_failed(server_name):
                conn_manager.reset_auth_failure(server_name)
            if not conn_manager.has_connection(server_name):
                conn_manager.register(config, server_name, srv)
            await conn_manager.connect(config, server_name, srv)
        proxy = _create_server_proxy(config, server_name, srv)
        combiner.mount(proxy, namespace=server_name)

    def _drop_providers(server_name: str) -> int:
        """Remove all mounted providers for *server_name*'s namespace.

        Returns the number removed. Does not touch connections or processes.
        """
        before = len(combiner.providers)

        def _matches(p: object) -> bool:
            r = repr(p)
            if f"namespace='{server_name}'" in r or f'namespace="{server_name}"' in r:
                return True
            return getattr(p, "_namespace", None) == server_name

        combiner.providers = [p for p in combiner.providers if not _matches(p)]
        return before - len(combiner.providers)

    @combiner.tool()
    async def combiner__restart_server(server_name: str) -> str:
        """Restart a single MCP server in place — ditch it and bring it back fresh.

        This is a true restart, not a refcount bounce: for sharedserver-backed
        servers the backing process is stopped (``sharedserver admin stop
        --force``: graceful SIGTERM, SIGKILL fallback) and respawned, rather than
        merely re-attaching to the same still-running process within its grace
        period. For HTTP/SSE servers the persistent upstream connection is torn
        down and re-opened; for plain stdio servers the FastMCP proxy is dropped
        and recreated. Other servers are left untouched.

        Use this when one server is wedged (hung, stale auth, crashed subprocess)
        and you want to kick just that one without restarting the whole combiner.

        Note: stopping a sharedserver-backed process clears its shared state, so
        any *other* clients attached to the same shared server will also see it
        bounce — that is inherent to a real restart.

        Args:
            server_name: Name of the server to restart.

        Returns:
            Status message.
        """
        if server_name not in config.servers:
            return f"Error: Server '{server_name}' not found"
        srv = config.servers[server_name]
        if srv.disabled:
            return (
                f"Server '{server_name}' is disabled — use combiner__enable_server "
                "to bring it up instead of restarting"
            )

        from mcp_combiner.server import clear_tool_cache, invalidate_tool_cache

        # 1. Tear down the combiner-side connection + mounted providers. We do NOT
        #    call ss_manager.ensure_stopped here — that decrements the refcount
        #    (grace-period reattach). The hard process stop happens in step 2.
        if conn_manager.is_http_server(srv) and conn_manager.has_connection(server_name):
            try:
                await conn_manager.disconnect(server_name)
            except Exception:
                logger.exception("restart: failed to disconnect '%s'", server_name)
        removed = _drop_providers(server_name)

        # 2. Hard-restart the backing process (no-op for non-sharedserver servers).
        restarted_proc = False
        try:
            restarted_proc = await ss_manager.restart(server_name)
        except Exception as e:
            logger.exception("restart: failed to restart backing process for '%s'", server_name)
            return f"Server '{server_name}': failed to restart backing process: {e}"

        # 3. Re-establish the connection and remount the proxy.
        try:
            await _mount_server(server_name, srv)
        except Exception as e:
            # The remount itself blew up — make sure no half-mounted, dead proxy
            # is left advertising tools, then notify clients to drop them.
            _drop_providers(server_name)
            invalidate_tool_cache()
            logger.exception("restart: failed to remount '%s'", server_name)
            return f"Server '{server_name}': backing process restarted but remount failed: {e}"

        # 4. Announce the refreshed tool list — but only once the upstream is
        #    actually reachable. For an HTTP server whose connection has not come
        #    back up yet, _mount_server still succeeds with a *disconnected*
        #    client (ConnectionManager.connect swallows the open failure). Telling
        #    clients the tools are ready in that state sends them calling into a
        #    dead proxy: ConnectionError → RetryMiddleware retries → "hanging".
        #    So we clear our local cache but defer the tools/list_changed
        #    notification to the reconnect monitor, which fires it via
        #    on_connected once the upstream is genuinely live.
        http = conn_manager.is_http_server(srv)
        connected = (not http) or conn_manager.is_connected(server_name)
        if connected:
            invalidate_tool_cache()
            ready_note = ""
        else:
            clear_tool_cache()
            ready_note = "; upstream still reconnecting — tools appear once it is live"

        proc_note = "process restarted" if restarted_proc else "connection re-opened"
        summary = (
            f"Server '{server_name}' restarted ({proc_note}; "
            f"{removed} provider(s) replaced){ready_note}"
        )
        logger.info(summary)
        return summary

    @combiner.tool()
    async def combiner__reload_config() -> str:
        """Re-read the config file and apply server changes without a restart.

        Diffs the on-disk config against the running config and applies the
        minimum work: servers added to the file are mounted (if enabled),
        removed servers are unmounted, and servers whose definition changed
        (including a toggled ``disabled`` flag) are remounted. Servers that are
        byte-for-byte unchanged keep their existing connection untouched.

        Reloads ``servers``, ``sharedServers``, and ``oauth`` from disk by
        mutating the live config object in place, so all holders (tool filter,
        health endpoint, meta-tools) see the new values.

        Returns:
            A summary of what changed.
        """
        from mcp_combiner.server import invalidate_tool_cache

        try:
            new_cfg = CombinerConfig.load(config.config_path)
        except Exception as e:
            logger.exception("reload_config: failed to read %s", config.config_path)
            return f"Error: failed to read config '{config.config_path}': {e}"

        old_servers = dict(config.servers)
        new_servers = new_cfg.servers

        added = set(new_servers) - set(old_servers)
        removed = set(old_servers) - set(new_servers)
        changed = {
            name
            for name in set(old_servers) & set(new_servers)
            if old_servers[name] != new_servers[name]
        }

        if not (added or removed or changed):
            return "No config changes detected."

        # 1) Tear down removed + changed servers while the OLD config is still
        #    live, so sharedServer / connection lookups resolve correctly.
        for name in removed | changed:
            try:
                await _unmount_server(name)
            except Exception:
                logger.exception("reload_config: failed to unmount '%s'", name)

        # 2) Swap in the new config in place (same object — preserves all the
        #    references handed to meta-tools, the lifespan, /health, and the
        #    _combiner_config global used for tool filtering).
        config.servers = new_cfg.servers
        config.shared_servers = new_cfg.shared_servers
        config.oauth = new_cfg.oauth

        # 3) Mount added + changed servers that are enabled, now reading the
        #    fresh definitions from the swapped-in config.
        mounted: list[str] = []
        failed: list[str] = []
        for name in added | changed:
            srv = new_servers[name]
            if srv.disabled:
                continue
            try:
                await _mount_server(name, srv)
                mounted.append(name)
            except Exception as e:
                failed.append(f"{name} ({e})")
                logger.exception("reload_config: failed to mount '%s'", name)

        invalidate_tool_cache()

        parts = [
            f"added={sorted(added)}",
            f"removed={sorted(removed)}",
            f"changed={sorted(changed)}",
            f"mounted={sorted(mounted)}",
        ]
        if failed:
            parts.append(f"failed={failed}")
        summary = "Config reloaded: " + ", ".join(parts)
        logger.info(summary)
        return summary

    @combiner.tool()
    async def combiner__session_disable_server(
        server_name: str, ctx: Context, chat_id: str | None = None
    ) -> str:
        """Disable an MCP server for the current session only.

        Unlike combiner__disable_server (which affects all sessions globally),
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

        from mcp_combiner.server import _session_disabled

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

        return json.dumps(
            {
                "session_id": sid,
                "action": "disabled",
                "server": server_name,
                "disabled_servers": sorted(_session_disabled.get(sid, set())),
            }
        )

    @combiner.tool()
    async def combiner__session_enable_server(
        server_name: str, ctx: Context, chat_id: str | None = None
    ) -> str:
        """Re-enable an MCP server that was disabled for the current session.

        Reverses the effect of combiner__session_disable_server for the
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

        from mcp_combiner.server import _session_disabled

        # Use chat_id if provided, otherwise fall back to MCP session_id
        sid = chat_id if chat_id else ctx.session_id
        blocked = _session_disabled.get(sid)
        if not blocked or server_name not in blocked:
            import json

            return json.dumps(
                {
                    "session_id": sid,
                    "action": "no_change",
                    "server": server_name,
                    "message": f"Server '{server_name}' is not session-disabled",
                    "disabled_servers": sorted(_session_disabled.get(sid, set())),
                }
            )

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

        return json.dumps(
            {
                "session_id": sid,
                "action": "enabled",
                "server": server_name,
                "disabled_servers": sorted(_session_disabled.get(sid, set())),
            }
        )

    @combiner.tool()
    async def combiner__session_status(ctx: Context, chat_id: str | None = None) -> str:
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
        from mcp_combiner.server import _session_disabled

        sid = chat_id if chat_id else ctx.session_id
        blocked = list(_session_disabled.get(sid, set()))
        blocked.sort()

        import json

        return json.dumps(
            {
                "session_id": sid,
                "disabled_servers": blocked,
            }
        )
