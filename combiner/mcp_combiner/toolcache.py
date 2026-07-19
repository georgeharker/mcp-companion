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
import os
import re
import time
from enum import Enum
from typing import Any

from fastmcp import FastMCP
from fastmcp.tools import Tool

from mcp_combiner.runtime import RUNTIME
from mcp_combiner.schemafix import _sanitize_tools

logger = logging.getLogger("mcp-combiner")


class PrimeOutcome(str, Enum):
    """Result of one started→ready prime attempt (see prime_server_tools)."""

    #: A real tool list came back; the slice is confirmed (published iff changed).
    READY = "ready"
    #: The upstream kept ANSWERING an empty list until the deadline — nothing
    #: stored, nothing published. Convergence comes from the monitor's re-probe
    #: (HTTP), the next aggregate fetch, or the upstream's own tools/list_changed.
    EMPTY = "empty"
    #: tools/list never answered within the deadline; server stays started.
    FAILED = "failed"


# Timeout for individual upstream server queries during tools/list
UPSTREAM_TOOL_LIST_TIMEOUT = 5.0  # seconds

# How long to keep priming a just-restarted stdio/sharedserver proxy's tools/list
# before giving up (the respawned process needs a moment to start serving).
# Tunable via env — an OAuth upstream warming up over a high-latency link can
# need well past the default; unset it and the historical 30s is unchanged.
try:
    _LOCAL_TOOLS_READY_TIMEOUT = float(os.environ["MCP_COMBINER_TOOLS_READY_TIMEOUT"])
except (KeyError, ValueError):
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


# ``protocol://path`` — mirrors FastMCP's Namespace URI pattern
# (fastmcp/server/transforms/namespace.py). Combiner-owned so we don't call the
# private ``Namespace._transform_uri``; pinned equal to it by a test so a FastMCP
# scheme change surfaces in CI rather than silently diverging.
_NS_URI = re.compile(r"^([^:]+://)(.*?)$")


def namespace_uri(uri: str, prefix: str) -> str:
    """Apply a namespace to a URI: ``protocol://path`` → ``protocol://<prefix>/path``.

    Identical to what the mount's ``Namespace`` applies to resources, so a
    namespaced notification / tool-``_meta`` pointer matches ``resources/list``.
    Non-URI strings pass through unchanged.
    """
    m = _NS_URI.match(uri)
    return f"{m.group(1)}{prefix}/{m.group(2)}" if m else uri


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
    then appends the cached slice for any *known* (confirmed) server that
    dropped out of this fetch while merely reconnecting — within
    ``RUNTIME.tools.stale_grace``. Servers that are removed from config,
    disabled, auth-failed, or past the grace window are dropped and evicted,
    so we never advertise uncallable tools. Only confirmed lists ever enter
    the store (an empty prime stores nothing), so everything here is
    last-known-good by construction.

    This is the hysteresis that stops one server's reconnect (which clears the
    whole cache) from blanking another server that happens to be mid-reconnect.

    Publication: a server observed here with tools that had no confirmed slice
    before (promotion — e.g. an isolate=true server that answered empty at
    boot and has now warmed up), or whose confirmed list changed in content,
    is a "new tool list back from the MCP". One notify-only broadcast is
    scheduled for the batch, AFTER the caller stores the merged result in the
    aggregate cache — sessions that re-fetch hit the fresh cache, so no clear
    is involved and re-broadcast cannot loop (an unchanged list returns False
    from store_slice).
    """
    per_fresh, _local = _partition_by_server(fresh)

    # Every server that returned tools this fetch is live: refresh its slice
    # and note whether that constitutes a publication.
    changed: list[str] = []
    for server, slice_ in per_fresh.items():
        if RUNTIME.tools.store_slice(server, slice_, now):
            changed.append(server)

    result = list(fresh)
    if RUNTIME.config is None:
        if changed:
            _schedule_promotion_notify(changed)
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
    if changed:
        _schedule_promotion_notify(changed)
    return result


def _schedule_promotion_notify(servers: list[str]) -> None:
    """Publish (notify-only) for slices promoted/replaced during a fetch merge.

    Scheduled as a task so it runs after the in-flight fetch completes and the
    merged result has been stored in the aggregate cache — the sessions this
    notification pokes will re-fetch into that fresh cache.
    """
    logger.info(
        "tools/list: publishing slice change for %s (promoted or replaced)",
        ", ".join(sorted(servers)),
    )
    notify_tool_list_changed()


def notify_tool_list_changed() -> None:
    """Broadcast ``tools/list_changed`` WITHOUT clearing the aggregate cache.

    The notify-only publication flavor: use when the cache already reflects
    the change (the aggregate-fetch merge fills ``set_cache`` with the fresh
    result right after promoting slices). Notified sessions re-fetch and hit
    that fresh cache; nothing is discarded.
    """
    # Fire-and-forget: callers are sync (merge tail, callbacks). The task ref
    # is kept in notification_tasks to prevent GC before completion.
    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(_notify_tool_list_changed())
        RUNTIME.notification_tasks.add(task)
        task.add_done_callback(RUNTIME.notification_tasks.discard)
    except RuntimeError:
        # No running event loop — skip notification (e.g. during tests)
        pass


def invalidate_tool_cache() -> None:
    """Invalidate the tool cache, forcing a refresh on next tools/list.

    Also sends ``notifications/tools/list_changed`` to all connected MCP
    clients so they re-fetch the tool list immediately. This is a
    *publication* — under the slice-state design it may only be driven by a
    ready-transition, a ready slice being replaced by a new list from the
    MCP, or a config-level event (enable/disable/restart/reload). See
    ``clear_tool_cache`` for the silent variant and
    ``notify_tool_list_changed`` for the notify-only flavor.
    """
    clear_tool_cache()
    notify_tool_list_changed()


async def prime_server_tools(
    combiner: FastMCP,
    name: str,
    timeout: float = _LOCAL_TOOLS_READY_TIMEOUT,
    interval: float = 1.0,
) -> PrimeOutcome:
    """Invoke ``tools/list`` on *name*'s mounted provider(s) and record the
    outcome in the confirmed-tools store.

    This is the started → ready transition attempt for a single server, and it
    is NOT automatic — a mounted server sits at "started" until something
    calls this (startup, enable, restart, the HTTP connection's tools-ready
    callback, or an upstream ``tools/list_changed``). A just-(re)spawned
    process is not immediately serving, so a non-answering OR answered-empty
    list is retried every *interval* until *timeout* — an empty 200 from a
    warming upstream (isolate=true servers at boot do this deterministically)
    is not readiness, and the retry is what guarantees a bounded trailing
    publication once the upstream's registry fills.

    Listing goes through the mounted provider (see mounts.get_server_providers),
    so the returned tools carry fastmcp's own namespacing — the exact shape an
    aggregate fetch produces. Outcomes:

    - Non-empty ``raw`` → READY: the sanitized + filtered slice is stored and,
      iff that confirmed a new server or changed the list's content, ONE
      ``tools/list_changed`` publication fires (clear + notify). An unchanged
      re-prime is broadcast-silent. Gating is on ``raw`` (pre-filter), so a
      tool_filter that legitimately filters to zero still confirms.
    - Only empty answers until *timeout* → EMPTY: nothing stored, nothing
      published — the last-known-good slice (if any) is untouched. Late
      convergence comes from the monitor's re-probe (HTTP), the next
      aggregate fetch (merge promotion), or the upstream's own
      ``tools/list_changed``.
    - No answer within *timeout* → FAILED: server stays started, no store, no
      broadcast.
    """
    from mcp_combiner.mounts import get_server_providers

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        providers = get_server_providers(combiner, name)
        if not providers:
            logger.warning("prime: '%s' has no mounted providers — nothing to list", name)
            return PrimeOutcome.FAILED

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
                return PrimeOutcome.FAILED
            await asyncio.sleep(interval)
            continue

        if not raw:
            # Answered, but empty: the upstream is up before its tool registry
            # is usable. Not readiness — retry like a non-answer, so the
            # eventual populated answer IS the trailing publication.
            if loop.time() >= deadline:
                logger.info(
                    "prime: '%s' answered only empty tools/list within %.0fs — "
                    "deferring (nothing stored or published; tools appear via "
                    "monitor re-probe, the next fetch, or upstream list_changed)",
                    name,
                    timeout,
                )
                return PrimeOutcome.EMPTY
            await asyncio.sleep(interval)
            continue

        filtered = _filter_tools(_sanitize_tools(raw))
        if RUNTIME.tools.store_slice(name, filtered, time.time()):
            logger.info(
                "prime: '%s' listed %d tool(s) — slice ready, publishing",
                name,
                len(filtered),
            )
            invalidate_tool_cache()
        else:
            logger.info(
                "prime: '%s' listed %d tool(s) — unchanged, no publication",
                name,
                len(filtered),
            )
        return PrimeOutcome.READY


def spawn_prime(combiner: FastMCP, name: str) -> asyncio.Task[PrimeOutcome]:
    """Run prime_server_tools in the background, keeping a strong task ref."""
    task = asyncio.get_running_loop().create_task(prime_server_tools(combiner, name))
    RUNTIME.prime_tasks.add(task)
    task.add_done_callback(RUNTIME.prime_tasks.discard)
    return task


def _on_upstream_tools_ready(name: str) -> None:
    """Tools-ready / tools-changed callback: run the shared, gated prime.

    Fired by the ConnectionManager probe (the upstream answers tools/list),
    the reconnect monitor, and the upstream ``tools/list_changed`` handler.
    Runs the shared prime — it re-lists through the mounted provider (warm
    connection) and records the outcome in the confirmed-tools store; whether
    anything is *published* is decided there (new confirmation or content
    replacement only). This keeps HTTP and stdio on ONE started → ready path.

    If the prime cannot run at all (combiner not up yet, no loop), fall back
    to a SILENT cache clear: no list was confirmed, so nothing may be
    published — the next fetch serves live truth and its merge promotion
    publishes if warranted.
    """
    combiner = RUNTIME.combiner
    if combiner is None:
        clear_tool_cache()
        return

    async def _prime_gated() -> None:
        if await prime_server_tools(combiner, name) is not PrimeOutcome.READY:
            # No confirmed list came back (never answered, or only empty
            # answers) — clear silently so the next fetch re-lists live;
            # publication waits for a confirmed list.
            clear_tool_cache()

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        clear_tool_cache()
        return
    task = loop.create_task(_prime_gated())
    RUNTIME.prime_tasks.add(task)
    task.add_done_callback(RUNTIME.prime_tasks.discard)
