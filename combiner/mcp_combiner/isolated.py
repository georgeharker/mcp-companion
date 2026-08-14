"""Token-keyed isolated upstream sessions.

For ``isolate: true`` servers the combiner opens a distinct upstream MCP
session per *chat*. fastmcp's ``StatefulProxyClient`` keys that cache by the
live downstream ``ServerSession`` object and force-disconnects the upstream
session the moment the downstream session exits — so any mid-chat downstream
reconnect (SSE drop, transport cycle) silently discards the chat's upstream
state even though nothing restarted.

``TokenKeyedStatefulClient`` re-keys the cache by the chat token (the
``x-mcp-combiner-session`` header that rides every tokened request): the same
chat reconnecting under a new downstream session reattaches to its existing
upstream client. Tokenless downstream sessions keep the inherited
session-object keying (today's behavior) — consistent with the accepted
supervised/unsupervised asymmetry.

Lifecycle (see the design decision "Isolated upstream sessions are
token-keyed with a live/parked/forgotten lifecycle"):

- LIVE: at least one downstream session bearing the token is active, or the
  last one exited less than ``GRACE_SECONDS`` ago. The upstream client stays
  connected through the grace window so quick reconnects reattach.
- On grace expiry the entry is torn down. Parking (disconnect *without*
  terminating the upstream session, keeping ``token -> Mcp-Session-Id`` for a
  later seeded resume) requires the resumable-transport work and lands with
  the live/parked/forgotten plan items; until then expiry is a full
  disconnect, exactly like the pre-token-keyed behavior at session exit.

State lives in the module-level ``REGISTRY`` (the ``nvim_proxy`` routing-table
pattern) so the handover/meta-tool layers can reach it without threading.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import time
import weakref
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fastmcp import Client
from fastmcp.client.transports.base import ClientTransportT
from fastmcp.server.dependencies import get_context, get_http_headers
from fastmcp.server.providers.proxy import StatefulProxyClient

if TYPE_CHECKING:
    from mcp.server.session import ServerSession

logger = logging.getLogger("mcp-combiner")

# The header carrying the chat token on every tokened request — sent by the
# client directly or injected from the /mcp/<token> URL by
# TokenRewriteMiddleware (see nvim_proxy.record_session_token).
TOKEN_HEADER = "x-mcp-combiner-session"

# How long an upstream client survives after the last downstream session
# bearing its token exits. Reconnects inside the window reattach; expiry
# tears down. (Config knobs arrive with the parked/forgotten lifecycle.)
GRACE_SECONDS = 300.0

# Sweep cadence for expiring idle entries.
SWEEP_INTERVAL_SECONDS = 30.0


def _request_token() -> str | None:
    """The chat token of the current request, if it carries one."""
    headers = get_http_headers()
    return headers.get(TOKEN_HEADER) or None


@dataclass
class LiveEntry:
    """A connected per-chat upstream client and the downstream sessions using it."""

    client: Client[Any]
    # Downstream sessions currently riding this upstream client. Weak: a
    # session that dies without a clean exit still drops out.
    sessions: weakref.WeakSet[ServerSession] = field(default_factory=weakref.WeakSet)
    # Monotonic time of the last factory acquire or downstream session exit;
    # the grace window counts from here once ``sessions`` is empty.
    last_activity: float = field(default_factory=time.monotonic)


class IsolatedSessionRegistry:
    """Live token-keyed upstream clients for all isolate:true servers.

    Keyed by ``(server, token)``. All mutation goes through the methods; the
    sweeper task expires idle entries after the grace window.
    """

    def __init__(self) -> None:
        self.live: dict[tuple[str, str], LiveEntry] = {}
        self._sweeper: asyncio.Task[None] | None = None

    # -- factory-side operations -------------------------------------------

    def acquire(
        self, server: str, token: str, session: ServerSession | None
    ) -> LiveEntry | None:
        """Return the live entry for (server, token), tracking the session.

        Touches ``last_activity`` (the factory runs per proxied call, so this
        doubles as the idle clock) and registers an exit hook the first time a
        downstream session is seen — the hook only stamps ``last_activity`` so
        the grace window counts from the exit, never tears anything down.
        """
        entry = self.live.get((server, token))
        if entry is None:
            return None
        entry.last_activity = time.monotonic()
        self._track_session(entry, session)
        return entry

    def register(
        self, server: str, token: str, client: Client[Any], session: ServerSession | None
    ) -> LiveEntry:
        """Record a freshly created per-chat client as the live entry."""
        entry = LiveEntry(client=client)
        self._track_session(entry, session)
        self.live[(server, token)] = entry
        self._ensure_sweeper()
        logger.debug("isolated: new upstream client for %s (token %s…)", server, token[:8])
        return entry

    def _track_session(self, entry: LiveEntry, session: ServerSession | None) -> None:
        if session is None or session in entry.sessions:
            return
        entry.sessions.add(session)

        async def _on_session_exit() -> None:
            # Grace, not teardown: the chat may reconnect under a new
            # downstream session and reattach by token.
            entry.last_activity = time.monotonic()

        session._exit_stack.push_async_callback(_on_session_exit)

    # -- teardown ----------------------------------------------------------

    async def evict_server(self, server: str) -> int:
        """Disconnect and drop every entry for one server (server restart).

        A restarted backing server's sessions are dead; chats must get clean
        fresh upstream sessions on their next call instead of erroring against
        stale ids. Returns the number of entries evicted.
        """
        keys = [k for k in self.live if k[0] == server]
        for key in keys:
            await self._teardown(key)
        return len(keys)

    async def close_all(self) -> None:
        """Disconnect everything (combiner shutdown)."""
        for key in list(self.live):
            await self._teardown(key)
        if self._sweeper is not None:
            self._sweeper.cancel()
            self._sweeper = None

    async def _teardown(self, key: tuple[str, str]) -> None:
        entry = self.live.pop(key, None)
        if entry is None:
            return
        try:
            await entry.client._disconnect(force=True)
        except Exception:
            logger.debug("isolated: disconnect failed for %s", key[0], exc_info=True)

    # -- sweeper -----------------------------------------------------------

    def _ensure_sweeper(self) -> None:
        if self._sweeper is not None and not self._sweeper.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # sync test context; expiry is exercised directly
        self._sweeper = loop.create_task(self._sweep_loop(), name="isolated-sweeper")

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            try:
                await self.sweep_once()
            except Exception:
                logger.exception("isolated: sweep failed")

    async def sweep_once(self, now: float | None = None) -> int:
        """Expire live entries idle past the grace window; returns count."""
        now = time.monotonic() if now is None else now
        expired = [
            key
            for key, entry in self.live.items()
            if not entry.sessions and now - entry.last_activity > GRACE_SECONDS
        ]
        for key in expired:
            logger.info(
                "isolated: expiring idle upstream session for %s (token %s…)",
                key[0],
                key[1][:8],
            )
            await self._teardown(key)
        return len(expired)

    def reset(self) -> None:
        """Drop all state without disconnecting (test isolation)."""
        self.live.clear()
        if self._sweeper is not None:
            self._sweeper.cancel()
            self._sweeper = None


REGISTRY = IsolatedSessionRegistry()


class TokenKeyedStatefulClient(StatefulProxyClient[ClientTransportT]):
    """``StatefulProxyClient`` whose per-chat cache is keyed by chat token.

    Tokened requests resolve their upstream client through ``REGISTRY`` by
    ``(server, token)`` — surviving downstream session churn. Tokenless
    requests fall through to the inherited per-``ServerSession`` cache with
    its disconnect-on-exit lifecycle.
    """

    def __init__(self, *args: Any, server_name: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._server_name = server_name

    def new_stateful(self) -> Client[ClientTransportT]:
        token = _request_token()
        if token is None:
            return super().new_stateful()

        try:
            session: ServerSession | None = get_context().session
        except Exception:
            session = None

        entry = REGISTRY.acquire(self._server_name, token, session)
        if entry is not None:
            return entry.client

        proxy_client = self.new()
        # Client.new() is a shallow copy sharing one transport wrapper object;
        # each connect overwrites the wrapper's get_session_id callback, so a
        # shared wrapper cannot answer "which upstream session is THIS chat's"
        # under concurrency. Give every per-chat client its own wrapper.
        proxy_client.transport = copy.copy(proxy_client.transport)
        REGISTRY.register(self._server_name, token, proxy_client, session)
        return proxy_client
