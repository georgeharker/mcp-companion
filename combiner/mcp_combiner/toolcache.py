"""tools/list cache, per-server hysteresis, priming, and change notification.

Moved verbatim from server.py during the decomposition. This module owns the
started → ready lifecycle for tool publication:

- ``clear_tool_cache`` (silent) vs ``invalidate_tool_cache`` (clears AND
  broadcasts ``tools/list_changed``) — the distinction is load-bearing; see
  each docstring.
- ``_merge_stale_server_tools`` — the stale-tool hysteresis
  (``RUNTIME.tools.stale_grace`` window) that stops one server's reconnect
  from blanking another mid-reconnect server's tools.
- ``prime_server_tools`` / ``spawn_prime`` / ``_on_upstream_tools_ready`` —
  the single started → ready transition shared by stdio, sharedserver and
  HTTP upstreams.
- session change notifications (``_notify_tool_list_changed`` /
  ``_notify_session_by_id``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from fastmcp import FastMCP
from fastmcp.tools import Tool

from mcp_combiner.runtime import RUNTIME
from mcp_combiner.schemafix import _sanitize_tools

logger = logging.getLogger("mcp-combiner")

# Timeout for individual upstream server queries during tools/list
UPSTREAM_TOOL_LIST_TIMEOUT = 5.0  # seconds

# How long to keep priming a just-restarted stdio/sharedserver proxy's tools/list
# before giving up (the respawned process needs a moment to start serving).
_LOCAL_TOOLS_READY_TIMEOUT = 30.0  # seconds
_LOCAL_TOOLS_READY_ATTEMPT = 5.0  # per-attempt bound, so a hung list can't block


async def _notify_tool_list_changed() -> None:
    """Send ``notifications/tools/list_changed`` to every active MCP session.

    Exceptions from individual sessions (e.g. client already disconnected)
    are logged and swallowed so one bad session never blocks the rest.
    """
    sessions = RUNTIME.sessions.sessions()
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
    for session in RUNTIME.sessions.sessions():
        try:
            sid = getattr(session, "_fastmcp_state_prefix", None) or str(id(session))
            if sid == session_id:
                await session.send_tool_list_changed()
                return
        except Exception:
            logger.debug("Failed to notify session %s", session_id, exc_info=True)


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
    if RUNTIME.config is None:
        return None, tool_name

    # Check each server name to see if the tool starts with it
    for server_name in RUNTIME.config.servers:
        prefix = server_name + "_"
        if tool_name.startswith(prefix):
            local_name = tool_name[len(prefix) :]
            return server_name, local_name

    return None, tool_name


def _is_transport_dead(exc: BaseException) -> bool:
    """True if *exc* signals the upstream process/transport is gone.

    Distinguishes a dead server (crashed stdio subprocess, broken pipe, closed
    stream, dropped connection) from an ordinary tool-level error. A dead
    transport marks the server down in ``RUNTIME.tools.failed_servers``; a tool error does
    not. Matches anyio's stream-closed exceptions by name to avoid importing it.
    """
    if isinstance(exc, (ConnectionError, BrokenPipeError, TimeoutError, EOFError, OSError)):
        return True
    return type(exc).__name__ in (
        "ClosedResourceError",
        "BrokenResourceError",
        "EndOfStream",
        "ProcessLookupError",
    )


def _filter_tools(tools: list[Tool]) -> list[Tool]:
    """Filter tools based on server-specific tool_filter patterns."""
    if RUNTIME.config is None:
        return tools

    filtered: list[Tool] = []
    for tool in tools:
        name = str(tool.name) if tool.name else ""

        server_name, local_name = _find_server_for_tool(name)

        if server_name is None:
            # Combiner tools (no server prefix) - always include
            filtered.append(tool)
            continue

        # Get server config
        srv = RUNTIME.config.servers.get(server_name)
        if srv is None or not srv.tool_filter:
            # No filter configured - include all tools from this server
            filtered.append(tool)
        elif _matches_filter(local_name, srv.tool_filter):
            # Matches filter - include
            filtered.append(tool)
        # else: doesn't match filter - exclude

    return filtered


def clear_tool_cache() -> None:
    """Clear the cached tool list + schema validators WITHOUT notifying clients.

    Use this when the tool set may have changed but the new upstream is not yet
    ready to serve calls. Clearing locally stops us serving stale entries, while
    *not* sending ``tools/list_changed`` keeps clients from re-fetching and then
    calling into a proxy whose connection is still down (which would surface as
    retries/"hanging"). The reconnect monitor fires the notification via
    ``on_tools_ready`` once the upstream's tools are actually listable.
    """
    RUNTIME.tools.clear_cache()
    # Drop cached JSON-schema validators so a reload or newly-connected server
    # never validates a tool call against a stale schema.
    from mcp_combiner import fastvalidate

    fastvalidate.clear_cache()
    logger.info("Tool cache cleared")


def _partition_by_server(tools: list[Tool]) -> tuple[dict[str, list[Tool]], list[Tool]]:
    """Split *tools* into per-server slices and a "local" bucket.

    The local bucket holds tools with no server prefix — the combiner's own
    meta-tools and the virtual ``neovim_*`` tools — which are always fresh and
    never subject to stale re-injection.
    """
    per: dict[str, list[Tool]] = {}
    local: list[Tool] = []
    for t in tools:
        server, _ = _find_server_for_tool(str(t.name) if t.name else "")
        if server is None:
            local.append(t)
        else:
            per.setdefault(server, []).append(t)
    return per, local


def _merge_stale_server_tools(fresh: list[Tool], now: float) -> list[Tool]:
    """Re-inject last-known-good tools for servers that are only transiently absent.

    Refreshes the per-server slice cache for every server present in *fresh*,
    then appends the cached slice for any *known* server that dropped out of
    this fetch while merely reconnecting — within ``RUNTIME.tools.stale_grace``. Servers
    that are removed from config, disabled, auth-failed, or past the grace
    window are dropped and evicted, so we never advertise uncallable tools.

    This is the hysteresis that stops one server's reconnect (which clears the
    whole cache) from blanking another server that happens to be mid-reconnect.
    """
    per_fresh, _local = _partition_by_server(fresh)

    # Every server that returned tools this fetch is live: refresh its slice and
    # confirm it "ready" (tools listable) for the stdio/sharedserver tri-state.
    for server, slice_ in per_fresh.items():
        RUNTIME.tools.store_slice(server, slice_, now)

    result = list(fresh)
    if RUNTIME.config is None:
        return result

    present = set(per_fresh)
    for server in list(RUNTIME.tools.server_tools):
        if server in present:
            continue
        srv = RUNTIME.config.servers.get(server)
        auth_failed = RUNTIME.conn_manager.is_auth_failed(server) if RUNTIME.conn_manager else False
        age = now - RUNTIME.tools.server_seen.get(server, 0.0)
        # Keep re-serving a merely-absent server's tools within the grace window
        # (transient reconnect — the flapping fix), EXCEPT once a tool call has
        # actually failed against it (recorded in _failed_servers): that is the
        # "remove on tool attempt" signal — a proven-dead upstream drops its tools
        # now rather than lingering for the full grace.
        eligible = (
            srv is not None
            and not srv.disabled
            and not auth_failed
            and server not in RUNTIME.tools.failed_servers
            and age < RUNTIME.tools.stale_grace
        )
        if eligible:
            stale = RUNTIME.tools.server_tools[server]
            result.extend(stale)
            logger.info(
                "tools/list: server '%s' absent (reconnecting) — serving %d "
                "last-known-good tool(s), %.0fs stale",
                server,
                len(stale),
                age,
            )
        else:
            # Removed, disabled, auth-failed, or grace expired — let it drop.
            RUNTIME.tools.evict_slice(server)
    return result


def invalidate_tool_cache() -> None:
    """Invalidate the tool cache, forcing a refresh on next tools/list.

    Also sends ``notifications/tools/list_changed`` to all connected MCP
    clients so they re-fetch the tool list immediately. Only call this once the
    affected upstream is actually reachable — see ``clear_tool_cache`` for the
    silent variant to use while a connection is still coming up.
    """
    clear_tool_cache()

    # Fire-and-forget notification to all connected sessions.
    # We schedule this as a task because invalidate_tool_cache() is called
    # from sync contexts (e.g. ConnectionManager.on_tools_ready callback).
    # The task is stored in _notification_tasks to prevent GC before completion.
    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(_notify_tool_list_changed())
        RUNTIME.notification_tasks.add(task)
        task.add_done_callback(RUNTIME.notification_tasks.discard)
    except RuntimeError:
        # No running event loop — skip notification (e.g. during tests)
        pass


async def prime_server_tools(
    combiner: FastMCP,
    name: str,
    timeout: float = _LOCAL_TOOLS_READY_TIMEOUT,
    interval: float = 1.0,
) -> bool:
    """Invoke ``tools/list`` on *name*'s mounted provider(s); on answer, store
    the slice, mark the server ready, and broadcast the change.

    This is the started → ready transition for a single server, and it is NOT
    automatic — a mounted server sits at "started" until something calls this
    (startup, enable, restart, or the HTTP connection's tools-ready callback).
    A just-(re)spawned process is not immediately serving, so we retry until
    *timeout*.

    Listing goes through the mounted provider (see mounts.get_server_providers),
    so the returned tools carry fastmcp's own namespacing — the exact shape an
    aggregate fetch produces. On success the slice is sanitized + filtered and
    stored in the per-server cache (so the tools are already known, not just
    known-to-exist), THEN the tool cache is invalidated, which broadcasts
    ``tools/list_changed``. Nothing is broadcast before the list returns, so
    clients are never told to re-fetch into a half-up server. Returns True on
    success; False on timeout (the server stays started-not-ready, no
    broadcast).
    """
    from mcp_combiner.mounts import get_server_providers

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        providers = get_server_providers(combiner, name)
        if not providers:
            logger.warning("prime: '%s' has no mounted providers — nothing to list", name)
            return False

        async def _list_all(provs: list[Any]) -> list[Tool]:
            out: list[Tool] = []
            for provider in provs:
                out.extend(await provider.list_tools())
            return out

        try:
            raw = await asyncio.wait_for(_list_all(providers), timeout=_LOCAL_TOOLS_READY_ATTEMPT)
        except Exception as e:
            if loop.time() >= deadline:
                logger.warning(
                    "prime: '%s' tools/list did not answer within %.0fs — deferring "
                    "(tools appear on the next successful fetch): %s",
                    name,
                    timeout,
                    e,
                )
                return False
            await asyncio.sleep(interval)
            continue

        filtered = _filter_tools(_sanitize_tools(raw))
        RUNTIME.tools.store_slice(name, filtered, time.time())
        logger.info(
            "prime: '%s' listed %d tool(s) — slice stored, invalidating cache",
            name,
            len(filtered),
        )
        invalidate_tool_cache()
        return True


def spawn_prime(combiner: FastMCP, name: str) -> asyncio.Task[bool]:
    """Run prime_server_tools in the background, keeping a strong task ref."""
    task = asyncio.get_running_loop().create_task(prime_server_tools(combiner, name))
    RUNTIME.prime_tasks.add(task)
    task.add_done_callback(RUNTIME.prime_tasks.discard)
    return task


def _on_upstream_tools_ready(name: str) -> None:
    """ConnectionManager tools-ready callback: prime, store, then broadcast.

    The connection's own probe just confirmed the upstream answers tools/list,
    so run the shared prime — it re-lists through the mounted provider (warm
    connection), stores the namespaced slice, and invalidates. This keeps HTTP
    and stdio on ONE started → ready path. If the prime can't run (combiner not
    up yet, no loop) or fails, fall back to the bare invalidation the callback
    used to do — the probe did confirm listability, so announcing is safe.
    """
    combiner = RUNTIME.combiner
    if combiner is None:
        invalidate_tool_cache()
        return

    async def _prime_or_announce() -> None:
        if not await prime_server_tools(combiner, name):
            invalidate_tool_cache()

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        invalidate_tool_cache()
        return
    task = loop.create_task(_prime_or_announce())
    RUNTIME.prime_tasks.add(task)
    task.add_done_callback(RUNTIME.prime_tasks.discard)
