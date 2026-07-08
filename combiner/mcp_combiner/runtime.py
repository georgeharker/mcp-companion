"""Combiner runtime state.

One object owning the mutable state that previously lived as ~15 module
globals in server.py. Introduced incrementally: server.py aliases its
historical global names to these containers (same object identity, so every
existing cross-module import keeps observing the same state) and modules
extracted from server.py take the runtime directly.

Layout mirrors the state's two concerns:

- ``SessionRegistry`` — everything keyed by/about downstream client sessions.
  NOTE (QUESTIONS.md Q1): ``token_sessions`` values are currently the wire
  ``mcp-session-id`` while filtering reads ``Context.session_id`` keys in
  ``disabled`` — two namespaces. Preserved as-is; do not "fix" in passing.
- ``ToolCacheState`` — the tools/list cache, per-server last-known-good
  hysteresis slices, started→ready tracking for local (stdio/sharedserver)
  servers, and lazy failure bookkeeping.
"""

from __future__ import annotations

import asyncio
import uuid
import weakref
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from fastmcp.tools import Tool
    from mcp.server.session import ServerSession

    from mcp_combiner.config import CombinerConfig
    from mcp_combiner.connections import ConnectionManager
    from mcp_combiner.sharedserver import SharedServerManager


@dataclass
class SessionRegistry:
    """Downstream (client ↔ combiner) session state."""

    # Weak refs to all active ServerSessions; populated per request by
    # ToolProcessingMiddleware, auto-pruned on GC.
    active: weakref.WeakSet[ServerSession] = field(default_factory=weakref.WeakSet)
    # Per-session server blocklist: session id -> disabled server names.
    disabled: dict[str, set[str]] = field(default_factory=dict)
    # Chat token -> combiner session id (see Q1 namespace caveat above).
    token_sessions: dict[str, str] = field(default_factory=dict)
    # Filters stored for a token before its client has connected.
    pending_token_filters: dict[str, set[str]] = field(default_factory=dict)


@dataclass
class ToolCacheState:
    """tools/list cache + per-server hysteresis + local readiness state."""

    # The combined tools/list cache and when it was filled.
    cache: list[Tool] | None = None
    cache_time: float = 0.0
    # Per-server last-known-good slices + when each was last seen live
    # (the stale-tool hysteresis that stops cross-server flapping).
    server_tools: dict[str, list[Tool]] = field(default_factory=dict)
    server_seen: dict[str, float] = field(default_factory=dict)
    # started→ready tri-state for servers WITHOUT a ConnectionManager entry
    # (stdio / sharedserver), mirroring _ManagedConnection._tools_ready.
    local_tools_ready: dict[str, bool] = field(default_factory=dict)
    # Lazy liveness: server name -> last error message from a failed call.
    failed_servers: dict[str, str] = field(default_factory=dict)


@dataclass
class CombinerRuntime:
    """All mutable combiner state plus late-bound references to the config,
    the FastMCP instance, and the managers. One instance per process today
    (the historical module-global arrangement); handed explicitly to the
    modules split out of server.py."""

    sessions: SessionRegistry = field(default_factory=SessionRegistry)
    tools: ToolCacheState = field(default_factory=ToolCacheState)

    # Late-bound in create_combiner.
    config: CombinerConfig | None = None
    combiner: FastMCP | None = None
    conn_manager: ConnectionManager | None = None
    ss_manager: SharedServerManager | None = None

    # Enabled schema fixes (frozen at server creation).
    schema_fixes: frozenset[str] = frozenset()

    # Unique per-process id surfaced via /health; a change signals a combiner
    # restart so clients re-register instances and token bindings.
    boot_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    # Strong refs to background tasks (asyncio only keeps weak ones).
    prime_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    notification_tasks: set[asyncio.Task[None]] = field(default_factory=set)


# The process-wide runtime. server.py and the modules extracted from it share
# this instance; tests may reset() it for isolation.
RUNTIME = CombinerRuntime()


def reset() -> None:
    """Clear all mutable state IN PLACE (test isolation).

    Container identities are preserved — server.py's historical global names
    alias these exact objects, so replacing them would silently fork state.
    """
    r = RUNTIME
    # WeakSet has no .clear() guarantee across versions of interest; rebuildable
    # only by discarding members.
    for sess in list(r.sessions.active):
        r.sessions.active.discard(sess)
    r.sessions.disabled.clear()
    r.sessions.token_sessions.clear()
    r.sessions.pending_token_filters.clear()
    r.tools.cache = None
    r.tools.cache_time = 0.0
    r.tools.server_tools.clear()
    r.tools.server_seen.clear()
    r.tools.local_tools_ready.clear()
    r.tools.failed_servers.clear()
    r.prime_tasks.clear()
    r.notification_tasks.clear()
