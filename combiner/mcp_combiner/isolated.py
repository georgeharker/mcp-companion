"""Token-keyed isolated upstream sessions with a live/parked/forgotten lifecycle.

For ``isolate: true`` servers the combiner opens a distinct upstream MCP
session per *chat*. fastmcp's ``StatefulProxyClient`` keys that cache by the
live downstream ``ServerSession`` object and force-disconnects the upstream
session the moment the downstream session exits — so any mid-chat downstream
reconnect (SSE drop, transport cycle) silently discarded the chat's upstream
state even though nothing restarted.

``TokenKeyedStatefulClient`` re-keys the cache by the chat token (the
``x-mcp-combiner-session`` header that rides every tokened request): the same
chat reconnecting under a new downstream session reattaches to its existing
upstream client. Tokenless downstream sessions keep the inherited
session-object keying (today's behavior) — consistent with the accepted
supervised/unsupervised asymmetry.

Lifecycle — three tiers, pure timers, no supervisor death signal (see the
design decision "Isolated upstream sessions are token-keyed with a
live/parked/forgotten lifecycle"):

- LIVE: a downstream session bearing the token is active, or the last one
  exited less than ``grace`` ago. The upstream client stays connected (a
  ``StatefulProxyClient`` clone holds its session open across context exits)
  so reconnects inside the window reattach with zero machinery.
- PARKED: grace expired. The upstream client is disconnected WITHOUT
  terminating the upstream session (``ResumableStreamableHttpTransport``'s
  close-time ``terminate_on_close=False``); only
  ``token -> (Mcp-Session-Id, protocolVersion)`` is kept. The token's next
  appearance resumes via a seeded transport, probed with ``ping`` and
  falling back to a fresh session if the server expired it. Non-resumable
  transports (SSE) skip parking: grace expiry is a full teardown.
- FORGOTTEN: parked past ``ttl``. The entry is dropped and a best-effort
  bare DELETE releases the upstream server's session state promptly (safe:
  a forgotten entry is unreachable by definition). The effective resume
  window is min(ttl, the server's own session expiry).

Every wrong timer guess self-corrects: park too early → resume; forget too
early → fresh session (the pre-token-keyed behavior).

State lives in the module-level ``REGISTRY`` (the ``nvim_proxy`` routing-table
pattern) so the handover/meta-tool layers can reach it without threading.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
import time
import weakref
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx
from fastmcp import Client
from fastmcp.client.transports.base import ClientTransportT
from fastmcp.server.dependencies import get_context, get_http_headers
from fastmcp.server.providers.proxy import StatefulProxyClient

from mcp_combiner.resume_transport import ResumableStreamableHttpTransport

if TYPE_CHECKING:
    from mcp.server.session import ServerSession

logger = logging.getLogger("mcp-combiner")

# The header carrying the chat token on every tokened request — sent by the
# client directly or injected from the /mcp/<token> URL by
# TokenRewriteMiddleware (see nvim_proxy.record_session_token).
TOKEN_HEADER = "x-mcp-combiner-session"

# Namespace prefix for group keys derived from the downstream wire
# Mcp-Session-Id (the tokenless tier's chat identity — per-chat on Claude
# Code, per-instance on OpenCode). Only sid: keys are alias-eligible after a
# combiner restart (the 404-correlation rename); explicit tokens are stored
# raw and re-associate by simply being presented again.
SID_PREFIX = "sid:"

# Defaults; overridden from CombinerConfig.isolation by create_combiner.
GRACE_SECONDS = 300.0
PARK_TTL_SECONDS = 3600.0

# Sweep cadence for the grace/ttl timers.
SWEEP_INTERVAL_SECONDS = 30.0


def _request_token() -> str | None:
    """The chat token of the current request, if it carries one."""
    headers = get_http_headers()
    return headers.get(TOKEN_HEADER) or None


def _request_group_key() -> str | None:
    """The current request's chat-grouping key.

    An explicit supervisor-minted token wins (strongest identity — survives
    client restarts). Otherwise the downstream wire ``Mcp-Session-Id`` names
    the chat: it is stable for the whole conversation on Claude Code (one MCP
    session per chat) and for the instance on OpenCode, and the
    404-correlation aliasing carries it across a sanctioned combiner restart.
    ``None`` (no HTTP context at all) falls back to session-object keying.
    """
    # get_http_headers strips mcp-session-id by default (it must not be
    # forwarded downstream); include it explicitly — here it is read as the
    # chat identity, never forwarded.
    headers = get_http_headers(include={"mcp-session-id"})
    token = headers.get(TOKEN_HEADER)
    if token:
        return token
    sid = headers.get("mcp-session-id")
    if sid:
        return SID_PREFIX + sid
    return None


@dataclass
class LiveEntry:
    """A connected per-chat upstream client and the downstream sessions using it.

    Liveness is tracked TWO ways because neither alone is reliable:
    ``session_ids`` counts sessions whose exit-stack callback has not yet
    fired (the authoritative clean-exit signal — fastmcp's session manager
    keeps strong refs to terminated ServerSessions, so GC alone never
    empties the weakset), while the weakset catches a session that died
    without unwinding its exit stack. The entry counts as idle when EITHER
    says no sessions remain.
    """

    client: Client[Any]
    # id(session) for each downstream session still inside its lifecycle;
    # removed by the session's own exit-stack callback.
    session_ids: set[int] = field(default_factory=set)
    # Weak belt-and-braces: a session that vanished without a clean exit.
    sessions: weakref.WeakSet[ServerSession] = field(default_factory=weakref.WeakSet)
    # Monotonic time of the last factory acquire or downstream session exit;
    # the grace window counts from here once no sessions remain.
    last_activity: float = field(default_factory=time.monotonic)

    def has_live_sessions(self) -> bool:
        return bool(self.session_ids) and bool(self.sessions)


@dataclass
class ParkedSession:
    """A disconnected-but-alive upstream session, resumable by its token."""

    session_id: str
    protocol_version: str | None
    # For the forget-time DELETE (and diagnostic value): where the session lives.
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    parked_at: float = field(default_factory=time.monotonic)


class IsolatedSessionRegistry:
    """Token-keyed upstream sessions for all isolate:true servers.

    ``live`` and ``parked`` are keyed by ``(server, token)``. All mutation
    goes through the methods; the sweeper task drives the grace/ttl timers.
    """

    def __init__(self) -> None:
        self.live: dict[tuple[str, str], LiveEntry] = {}
        self.parked: dict[tuple[str, str], ParkedSession] = {}
        self.grace: float = GRACE_SECONDS
        self.ttl: float = PARK_TTL_SECONDS
        self._sweeper: asyncio.Task[None] | None = None
        # Strong refs to short-lived expedited sweeps (asyncio keeps weak ones).
        self._expedite_tasks: set[asyncio.Task[None]] = set()

    def configure(self, grace: float, ttl: float) -> None:
        self.grace = grace
        self.ttl = ttl

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
        """Record a per-chat client as the live entry."""
        entry = LiveEntry(client=client)
        self._track_session(entry, session)
        self.live[(server, token)] = entry
        self._ensure_sweeper()
        logger.debug("isolated: live upstream client for %s (token %s…)", server, token[:8])
        return entry

    def pop_parked(self, server: str, token: str) -> ParkedSession | None:
        """Claim a parked session for resumption (removes it)."""
        return self.parked.pop((server, token), None)

    def expedite_park(self, token: str) -> None:
        """Skip the grace window for a token whose client sent an explicit
        session DELETE — it declared this downstream session finished, so
        there is no quick-reconnect to keep a warm client for. Park (never
        forget): DELETE is ambiguous between chat-done and a clean transport
        cycle, and a parked entry serves both.

        Backdates the idle clock and kicks a near-immediate sweep; the sweep
        still requires the live-session set to be empty, so a token with
        OTHER downstream sessions still attached is untouched.
        """
        backdated = False
        now = time.monotonic()
        for (server, tok), entry in self.live.items():
            if tok == token:
                entry.last_activity = now - self.grace - 1.0
                backdated = True
                logger.debug("isolated: DELETE expedites park for %s (token %s…)", server, tok[:8])
        if backdated:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            # The DELETE's session teardown must finish (and the ServerSession
            # be collected out of the weakset) before a sweep can park; retry
            # a few times rather than waiting for the slow background cadence.
            async def _sweep_soon() -> None:
                for delay in (0.5, 1.0, 2.0):
                    await asyncio.sleep(delay)
                    try:
                        await self.sweep_once()
                    except Exception:
                        logger.exception("isolated: expedited sweep failed")
                        return
                    if not any(tok == token for (_, tok) in self.live):
                        return

            task = loop.create_task(_sweep_soon(), name="isolated-expedite")
            self._expedite_tasks.add(task)
            task.add_done_callback(self._expedite_tasks.discard)

    def restore_parked(self, server: str, token: str, parked: ParkedSession) -> None:
        """Load a parked entry (handover restore, or a failed claim put back)."""
        self.parked[(server, token)] = parked
        self._ensure_sweeper()

    # -- 404-correlation aliasing (wire-id group keys) ---------------------

    def has_parked_sid(self, sid: str) -> bool:
        """Whether any parked entry belongs to this old wire session id.

        This IS the roster of restorable identities — self-cleaning, because
        forget/TTL removes the parked entries themselves.
        """
        key = SID_PREFIX + sid
        return any(tok == key for (_, tok) in self.parked)

    def rename_sid(self, old_sid: str, new_sid: str) -> int:
        """Re-key parked entries from an old wire session id to the re-minted
        one — the chat formerly called *old_sid* is now called *new_sid*.

        The successor cannot fabricate the old downstream session, but it can
        hand the NEW session the old identity's parked state; the next
        isolated call then resumes lazily as usual. Returns entries renamed.
        """
        old_key, new_key = SID_PREFIX + old_sid, SID_PREFIX + new_sid
        moved = 0
        for server, tok in list(self.parked):
            if tok == old_key:
                self.parked[(server, new_key)] = self.parked.pop((server, old_key))
                moved += 1
        if moved:
            logger.info(
                "isolated: re-associated %d parked session(s): sid %s… → %s…",
                moved,
                old_sid[:8],
                new_sid[:8],
            )
        return moved

    def _track_session(self, entry: LiveEntry, session: ServerSession | None) -> None:
        if session is None:
            return
        sid = id(session)
        if sid in entry.session_ids:
            return
        entry.session_ids.add(sid)
        entry.sessions.add(session)

        async def _on_session_exit() -> None:
            # Grace, not teardown: the chat may reconnect under a new
            # downstream session and reattach by token.
            entry.session_ids.discard(sid)
            entry.last_activity = time.monotonic()

        session._exit_stack.push_async_callback(_on_session_exit)

    # -- park / forget -----------------------------------------------------

    async def park(self, key: tuple[str, str]) -> bool:
        """Disconnect a live entry, keeping its upstream session resumable.

        Only possible when the client rides a ``ResumableStreamableHttpTransport``
        with a known upstream session id; otherwise (SSE, no id captured) the
        teardown is full — grace expiry then behaves exactly like the old
        disconnect-on-session-exit. Returns True when the session was parked.
        """
        entry = self.live.pop(key, None)
        if entry is None:
            return False
        transport = entry.client.transport
        session_id: str | None = None
        if isinstance(transport, ResumableStreamableHttpTransport):
            session_id = transport.get_session_id() or transport.resume_session_id
        if session_id is None:
            await self._disconnect(entry, key)
            return False

        # Protocol version: from the initialize exchange on fresh sessions,
        # or carried on the transport for sessions that were themselves
        # resumed (auto_initialize=False never fills initialize_result).
        init = entry.client.initialize_result
        protocol_version = (
            str(init.protocolVersion) if init is not None else transport.resume_protocol_version
        )

        transport.terminate_on_close = False
        await self._disconnect(entry, key)
        self.parked[key] = ParkedSession(
            session_id=session_id,
            protocol_version=protocol_version,
            url=transport.url,
            headers=dict(transport.headers),
        )
        logger.info(
            "isolated: parked upstream session for %s (token %s…)", key[0], key[1][:8]
        )
        return True

    async def forget(self, key: tuple[str, str]) -> None:
        """Drop a parked entry and release the upstream session state.

        The DELETE is best-effort and safe: a forgotten entry is unreachable
        by definition, so nobody can resume what we terminate here.
        """
        parked = self.parked.pop(key, None)
        if parked is None:
            return
        logger.info(
            "isolated: forgetting parked session for %s (token %s…)", key[0], key[1][:8]
        )
        with contextlib.suppress(Exception):
            async with httpx.AsyncClient() as http:
                await http.delete(
                    parked.url,
                    headers={**parked.headers, "mcp-session-id": parked.session_id},
                    timeout=5.0,
                )

    # -- teardown ----------------------------------------------------------

    async def evict_server(self, server: str) -> int:
        """Drop every live and parked entry for one server (server restart).

        A restarted backing server's sessions are dead; chats must get clean
        fresh upstream sessions on their next call instead of erroring against
        stale ids. No DELETE is attempted — the sessions died with the server.
        Returns the number of entries evicted.
        """
        live_keys = [k for k in self.live if k[0] == server]
        for key in live_keys:
            entry = self.live.pop(key, None)
            if entry is not None:
                transport = entry.client.transport
                if isinstance(transport, ResumableStreamableHttpTransport):
                    transport.terminate_on_close = False  # session already dead
                await self._disconnect(entry, key)
        parked_keys = [k for k in self.parked if k[0] == server]
        for key in parked_keys:
            self.parked.pop(key, None)
        return len(live_keys) + len(parked_keys)

    async def close_all(self, terminate: bool = True) -> None:
        """Disconnect everything (combiner shutdown).

        ``terminate=False`` is the sanctioned-handover path: upstream sessions
        are left alive for the successor to resume (their ids travel in the
        handover payload); the default terminates them, today's behavior.
        """
        for key in list(self.live):
            if terminate:
                entry = self.live.pop(key, None)
                if entry is not None:
                    await self._disconnect(entry, key)
            else:
                await self.park(key)
        if self._sweeper is not None:
            self._sweeper.cancel()
            self._sweeper = None

    async def _disconnect(self, entry: LiveEntry, key: tuple[str, str]) -> None:
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
            # Track the timers: sub-second grace (tests) sweeps sub-second.
            await asyncio.sleep(
                min(SWEEP_INTERVAL_SECONDS, max(self.grace / 2, 0.1), max(self.ttl / 2, 0.1))
            )
            try:
                await self.sweep_once()
            except Exception:
                logger.exception("isolated: sweep failed")

    async def sweep_once(self, now: float | None = None) -> int:
        """Run both timers: park grace-expired live entries, forget stale
        parked ones. Returns the number of transitions."""
        now = time.monotonic() if now is None else now
        transitions = 0
        for key, entry in list(self.live.items()):
            if not entry.has_live_sessions() and now - entry.last_activity > self.grace:
                await self.park(key)
                transitions += 1
        for key, parked in list(self.parked.items()):
            if now - parked.parked_at > self.ttl:
                await self.forget(key)
                transitions += 1
        return transitions

    def reset(self) -> None:
        """Drop all state without disconnecting (test isolation)."""
        self.live.clear()
        self.parked.clear()
        self.grace = GRACE_SECONDS
        self.ttl = PARK_TTL_SECONDS
        if self._sweeper is not None:
            self._sweeper.cancel()
            self._sweeper = None
        for task in list(self._expedite_tasks):
            task.cancel()
        self._expedite_tasks.clear()


REGISTRY = IsolatedSessionRegistry()


class TokenKeyedStatefulClient(StatefulProxyClient[ClientTransportT]):
    """``StatefulProxyClient`` whose per-chat cache is keyed by chat token.

    Tokened requests resolve their upstream client through ``REGISTRY`` by
    ``(server, token)`` — surviving downstream session churn — and resume
    parked upstream sessions via a seeded transport. Tokenless requests fall
    through to the inherited per-``ServerSession`` cache with its
    disconnect-on-exit lifecycle.

    ``acquire_stateful`` (async, so the parked-resume probe can await) is the
    factory to hand to the proxy; ``new_stateful`` remains the sync
    tokenless fallback.
    """

    def __init__(self, *args: Any, server_name: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._server_name = server_name

    async def acquire_stateful(self) -> Client[Any]:
        token = _request_group_key()
        if token is None:
            return super().new_stateful()

        try:
            session: ServerSession | None = get_context().session
        except Exception:
            session = None

        entry = REGISTRY.acquire(self._server_name, token, session)
        if entry is not None:
            return entry.client

        parked = REGISTRY.pop_parked(self._server_name, token)
        if parked is not None:
            client = await self._try_resume(parked)
            if client is not None:
                REGISTRY.register(self._server_name, token, client, session)
                logger.info(
                    "isolated: resumed upstream session for %s (token %s…)",
                    self._server_name,
                    token[:8],
                )
                return client

        proxy_client = self._new_per_chat()
        REGISTRY.register(self._server_name, token, proxy_client, session)
        return proxy_client

    def _new_per_chat(self) -> Client[Any]:
        """A fresh per-chat clone with its own transport wrapper.

        Client.new() is a shallow copy sharing one transport wrapper object;
        each connect overwrites the wrapper's get_session_id callback, so a
        shared wrapper cannot answer "which upstream session is THIS chat's"
        under concurrency. Give every per-chat client a private wrapper.
        """
        proxy_client = self.new()
        proxy_client.transport = copy.copy(proxy_client.transport)
        return proxy_client

    async def _try_resume(self, parked: ParkedSession) -> Client[Any] | None:
        """Reattach to a parked upstream session; None means fall back fresh.

        The probe is one ping on the seeded connection: an expired/unknown id
        fails loudly (the server 404s) instead of silently minting a session,
        so a dead entry costs one round-trip and the caller builds fresh.
        """
        if not isinstance(self.transport, ResumableStreamableHttpTransport):
            return None
        client = self._new_per_chat()
        transport = client.transport
        assert isinstance(transport, ResumableStreamableHttpTransport)
        transport.resume_session_id = parked.session_id
        transport.resume_protocol_version = parked.protocol_version
        client.auto_initialize = False
        try:
            async with client:
                await client.ping()
        except Exception as exc:
            logger.info(
                "isolated: resume of %s session %s… failed (%s) — starting fresh",
                self._server_name,
                parked.session_id[:8],
                exc,
            )
            transport.terminate_on_close = False  # never DELETE a session we don't own
            with contextlib.suppress(Exception):
                await client._disconnect(force=True)
            return None
        # A StatefulProxyClient clone stays connected after the context exits,
        # so the probed connection is the chat's live upstream session.
        return client
