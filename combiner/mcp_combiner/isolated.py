"""Token-keyed isolated upstream sessions with a live/parked/forgotten lifecycle.

For ``isolate: true`` servers the combiner opens a distinct upstream MCP
session per *chat*. fastmcp's ``StatefulProxyClient`` keys that cache by the
live downstream ``Connection`` and force-disconnects the upstream
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

**stdio (process-per-chat) tier**: ``isolate: true`` on a *stdio* server means
there is nothing to key by transport — one subprocess is one session. The same
token keys map to per-chat SUBPROCESSES instead: grace expiry "parks" the
per-chat client by leaving its subprocess running (its state lives in-process —
the direct analog of keeping an HTTP server-side session), the ttl "forgets" by
terminating it, and a re-appearing token within the window reattaches to the
same live process. On combiner shutdown (or sanctioned handover) stdio
subprocesses are always terminated — they cannot transfer to a successor.
A per-chat subprocess that crashes surfaces as a tool error for that chat;
the chat's NEXT call detects the dead child (the same liveness signal
fastmcp's stdio transport uses) and heals it transparently — a fresh child
with fresh state; only the call that was in flight when the child died
fails. In-memory state is not preserved across a crash (the child IS the
session).
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
import time
import weakref
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, cast

import httpx2
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from fastmcp.client.transports.base import ClientTransportT
from fastmcp.server.dependencies import get_context, get_http_headers
from fastmcp.server.providers.proxy import StatefulProxyClient

from mcp_combiner.resume_transport import ResumableStreamableHttpTransport
from mcp_combiner.runtime import CLIENT_CONNECT_MODE, downstream_connection

if TYPE_CHECKING:
    from mcp.server.connection import Connection

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


def _request_connection() -> Connection | None:
    """The current request's downstream connection, outside a request ``None``."""
    try:
        return downstream_connection(get_context().session)
    except Exception:
        return None


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


class _Closable(Protocol):
    async def close(self) -> None:
        """Tear down the session and close the transport."""


async def _close_client(client: Client[Any]) -> None:
    """``Client.close()`` = session teardown + ``transport.close()``.

    For stdio that transport close is what terminates the keep_alive child,
    so this (not ``_disconnect``) is the teardown that matters. fastmcp
    declares the whole chain unannotated; the Protocol states the contract
    it actually has.
    """
    await cast(_Closable, client).close()


@dataclass
class LiveEntry:
    """A connected per-chat upstream client and the downstream sessions using it.

    A downstream session is its mcp ``Connection`` (see
    ``runtime.downstream_connection``). Liveness is tracked TWO ways because
    neither alone is reliable: ``session_ids`` counts connections whose
    exit-stack callback has not yet fired (the authoritative clean-exit
    signal — GC alone can lag a terminated session), while the weakset
    catches a connection that died without unwinding its exit stack. The
    entry counts as idle when EITHER says no sessions remain.
    """

    client: Client[Any]
    # id(connection) for each downstream session still inside its lifecycle;
    # removed by the connection's own exit-stack callback.
    session_ids: set[int] = field(default_factory=set)
    # Weak belt-and-braces: a connection that vanished without a clean exit.
    sessions: weakref.WeakSet[Connection] = field(default_factory=weakref.WeakSet)
    # Monotonic time of the last factory acquire or downstream session exit;
    # the grace window counts from here once no sessions remain.
    last_activity: float = field(default_factory=time.monotonic)

    def has_live_sessions(self) -> bool:
        return bool(self.session_ids) and bool(self.sessions)


@dataclass
class ParkedEntry(ABC):
    """A parked per-chat identity (the registry's second tier).

    The kind's forget/teardown behavior lives HERE (polymorphic ``release``);
    the registry's sweep/forget/close loops are uniform over this base and
    never branch on which transport a server rides.
    """

    #: monotonic time the entry was parked — the ttl-forgotten timer's anchor.
    #: kw_only so subclasses' required fields can follow the defaulted base
    #: field (dataclass ordering rule).
    parked_at: float = field(default_factory=time.monotonic, kw_only=True)

    #: whether this parked identity can transfer to a successor combiner
    #: (the sanctioned-handover shutdown keeps transferable entries; stdio
    #: subprocesses die with the process and so can only be terminated).
    transfers: ClassVar[bool]

    @abstractmethod
    def describe(self) -> str:
        """What this parked entry preserves, for log lines."""

    @abstractmethod
    async def release(
        self, registry: IsolatedSessionRegistry, key: tuple[str, str], evict: bool = False
    ) -> None:
        """Forget/evict this entry — release the state it preserves.

        ``evict=True`` marks a server-restart teardown: nothing survives the
        restart, so an HTTP session must NOT be DELETEd (release without the
        flag does, for a genuine forget). stdio terminates either way."""


@dataclass
class ParkedSession(ParkedEntry):
    """A disconnected-but-alive upstream session, resumable by its token."""

    #: HTTP sessions can transfer: the successor resumes them via session id.
    transfers: ClassVar[bool] = True
    session_id: str
    protocol_version: str | None
    # For the forget-time DELETE (and diagnostic value): where the session lives.
    url: str
    headers: dict[str, str] = field(default_factory=dict)

    def describe(self) -> str:
        return "upstream session"

    async def release(
        self, registry: IsolatedSessionRegistry, key: tuple[str, str], evict: bool = False
    ) -> None:
        """Release the upstream session state (best-effort DELETE).

        The DELETE is safe: a forgotten entry is unreachable by definition,
        so nobody can resume what we terminate here. With ``evict=True`` the
        session died with the restarted server — skip the DELETE."""
        if evict:
            return
        with contextlib.suppress(Exception):
            async with httpx2.AsyncClient() as http:
                await http.delete(
                    self.url,
                    headers={**self.headers, "mcp-session-id": self.session_id},
                    timeout=5.0,
                )


@dataclass
class ParkedProc(ParkedEntry):
    """A still-RUNNING per-chat stdio subprocess, parked past its grace window.

    Unlike an HTTP parked session there is nothing to disconnect-and-resume:
    the subprocess holds the chat's state in memory, so parking means keeping
    it alive (the analog of preserving the server-side session). The ttl
    forget terminates it.
    """

    #: subprocesses cannot transfer: they die with the combiner's process.
    transfers: ClassVar[bool] = False
    client: Client[Any]

    def describe(self) -> str:
        return "stdio process"

    async def release(
        self, registry: IsolatedSessionRegistry, key: tuple[str, str], evict: bool = False
    ) -> None:
        """Forget/evict both terminate the parked child (its state dies with
        it either way)."""
        await registry._disconnect(LiveEntry(client=self.client), key)


class IsolatedSessionRegistry:
    """Token-keyed upstream sessions for all isolate:true servers.

    ``live`` and ``parked`` are keyed by ``(server, token)``. All mutation
    goes through the methods; the sweeper task drives the grace/ttl timers.
    """

    def __init__(self) -> None:
        self.live: dict[tuple[str, str], LiveEntry] = {}
        # Parked second tier — POLYMORPHIC: one bucket for both kinds
        # (ParkedSession for HTTP/SSE, ParkedProc for stdio). The kind's
        # forget/teardown behavior rides the entry itself (``release``), so
        # the registry's loops never branch on transport.
        self.parked: dict[tuple[str, str], ParkedEntry] = {}
        # Tokenless-fallback BASE subprocesses per isolated stdio server (the
        # TokenKeyedStdioClient's own transport, shared by tokenless chats).
        # Tracked so evict/close_all can close them — keep_alive would
        # otherwise orphan the base child on restart/shutdown.
        self.bases: dict[str, list[Client[Any]]] = {}
        self.grace: float = GRACE_SECONDS
        self.ttl: float = PARK_TTL_SECONDS
        self._sweeper: asyncio.Task[None] | None = None
        # Strong refs to short-lived expedited sweeps (asyncio keeps weak ones).
        self._expedite_tasks: set[asyncio.Task[None]] = set()
        # Deferred dead-child closes (``_defer_close_dead``) — kept separate so
        # ``close_all``'s drain waits ONLY for these (bounded, fast) and never
        # for ``expedite_park``'s sweep-retry sleeps.
        self._dead_close_tasks: set[asyncio.Task[None]] = set()

    def configure(self, grace: float, ttl: float) -> None:
        self.grace = grace
        self.ttl = ttl

    # -- factory-side operations -------------------------------------------

    def acquire(self, server: str, token: str, session: Connection | None) -> LiveEntry | None:
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
        self, server: str, token: str, client: Client[Any], session: Connection | None
    ) -> LiveEntry:
        """Record a per-chat client as the live entry."""
        entry = LiveEntry(client=client)
        self._track_session(entry, session)
        self.live[(server, token)] = entry
        self._ensure_sweeper()
        logger.debug("isolated: live upstream client for %s (token %s…)", server, token[:8])
        return entry

    def pop_parked(self, server: str, token: str) -> ParkedEntry | None:
        """Claim a parked entry for resumption (removes it)."""
        return self.parked.pop((server, token), None)

    def register_base(self, server: str, client: Client[Any]) -> None:
        """Track an isolated stdio server's tokenless-fallback base client."""
        self.bases.setdefault(server, []).append(client)
        self._ensure_sweeper()

    async def _close_bases(self, server: str | None = None) -> int:
        """Close tracked base clients (all servers, or one). Returns the count.

        keep_alive would otherwise leave the shared tokenless child running
        after a restart (client dropped, child orphaned) or at shutdown.
        Awaited by evict_server/close_all so shutdown cannot exit first.
        """
        keys = [server] if server is not None else list(self.bases)
        closed = 0
        for key in keys:
            for client in self.bases.pop(key, []):
                if isinstance(getattr(client, "transport", None), StdioTransport):
                    try:
                        # fastmcp's close() is untyped upstream.
                        await _close_client(client)
                        closed += 1
                    except Exception:
                        logger.debug("isolated: base close failed for %s", key, exc_info=True)
        return closed

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

            # The DELETE's session teardown must finish (and the Connection
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
        """Load a parked HTTP/SSE entry (the handover-restore path: the
        successor boot replays the predecessor's parked sessions)."""
        self.parked[(server, token)] = parked
        self._ensure_sweeper()

    # NOTE: restored sid:-keyed parked entries are UNREACHABLE by design —
    # the wire id died with the predecessor and no client will present it
    # again (the 404-correlation re-association was prototyped and rejected;
    # see the design graph). They are still written/restored on purpose: the
    # successor's TTL sweep forgets them WITH the cleanup DELETE, releasing
    # the upstream server's state promptly instead of leaving it to the
    # server's own expiry.

    def _track_session(self, entry: LiveEntry, session: Connection | None) -> None:
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

        session.exit_stack.push_async_callback(_on_session_exit)

    # -- park / forget -----------------------------------------------------

    async def park(self, key: tuple[str, str]) -> bool:
        """Park a live entry — the kind's representation decides what parking
        means (HTTP/SSE: disconnect keeping the session resumable; stdio: keep
        the subprocess running). Non-parkable clients (SSE without a captured
        id) get a full teardown instead — grace expiry then behaves exactly
        like the old disconnect-on-session-exit. Returns True when parked."""
        entry = self.live.pop(key, None)
        if entry is None:
            return False
        parked = await self._park_entry(entry, key, transfer=False)
        if parked is None:
            return False  # full teardown already happened
        self.parked[key] = parked
        logger.info(
            "isolated: parked %s for %s (token %s…)",
            parked.describe(),
            key[0],
            key[1][:8],
        )
        return True

    async def _park_entry(
        self, entry: LiveEntry, key: tuple[str, str], transfer: bool
    ) -> ParkedEntry | None:
        """Build the parked representation for *entry* — the ONE kind-aware
        park site in the registry.

        ``transfer`` marks the sanctioned-handover shutdown: stdio
        subprocesses cannot transfer to a successor, so they terminate even
        though the caller says ``terminate=False`` (a grace-time park keeps
        them — ``transfer=False``). HTTP/SSE parks identically in both cases
        (session id survives via ``terminate_on_close=False``).

        Returns the parked entry, or ``None`` after a full teardown (nothing
        resumable to keep).
        """
        transport = entry.client.transport
        if isinstance(transport, StdioTransport):
            if transfer:
                await self._disconnect(entry, key)  # cannot transfer — terminate
            return ParkedProc(client=entry.client) if not transfer else None
        session_id: str | None = None
        if isinstance(transport, ResumableStreamableHttpTransport):
            session_id = transport.get_session_id() or transport.resume_session_id
        if session_id is None:
            await self._disconnect(entry, key)
            return None

        # Protocol version: from the initialize exchange on fresh sessions,
        # or carried on the transport for sessions that were themselves
        # resumed (auto_initialize=False never fills initialize_result).
        init = entry.client.initialize_result
        protocol_version = (
            str(init.protocol_version) if init is not None else transport.resume_protocol_version
        )

        transport.terminate_on_close = False
        await self._disconnect(entry, key)
        return ParkedSession(
            session_id=session_id,
            protocol_version=protocol_version,
            url=transport.url,
            headers=dict(transport.headers),
        )

    async def forget(self, key: tuple[str, str]) -> None:
        """Drop a parked entry and release the state it preserves — best-effort
        (DELETE for an HTTP session, terminate for a stdio subprocess). Safe:
        a forgotten entry is unreachable by definition."""
        parked = self.parked.pop(key, None)
        if parked is None:
            return
        logger.info(
            "isolated: forgetting %s for %s (token %s…)",
            parked.describe(),
            key[0],
            key[1][:8],
        )
        await parked.release(self, key)

    # -- teardown ----------------------------------------------------------

    async def evict_server(self, server: str) -> int:
        """Drop every live and parked entry for one server (server restart).

        A restarted backing server's sessions are dead; chats must get clean
        fresh upstream sessions on their next call instead of erroring against
        stale ids. No DELETE is attempted — the sessions died with the server
        (the ``evict`` flag tells the parked representation that); stdio
        children terminate either way (release IS terminating them).
        Returns the number of chat sessions/processes evicted.
        """
        live_keys = [k for k in self.live if k[0] == server]
        for key in live_keys:
            entry = self.live.pop(key, None)
            if entry is not None:
                await self._disconnect(entry, key, dropped_sessions=True)
        parked_keys = [k for k in self.parked if k[0] == server]
        for key in parked_keys:
            parked = self.parked.pop(key, None)
            if parked is not None:
                await parked.release(self, key, evict=True)
        closed_bases = await self._close_bases(server)
        if closed_bases:
            logger.info(
                "isolated: closed %d tokenless base child(ren) for %s", closed_bases, server
            )
        return len(live_keys) + len(parked_keys)

    async def close_all(self, terminate: bool = True) -> None:
        """Disconnect everything (combiner shutdown).

        ``terminate=False`` is the sanctioned-handover path: upstream sessions
        are left alive for the successor to resume (their ids travel in the
        handover payload); the default terminates them, today's behavior.
        """
        for key in list(self.live):
            entry = self.live.get(key)
            if entry is None:
                continue
            self.live.pop(key, None)
            if not terminate:
                # Sanctioned handover: park what can transfer — stdio
                # subprocesses cannot (transfer=True terminates them inside).
                parked = await self._park_entry(entry, key, transfer=True)
                if parked is not None:
                    self.parked[key] = parked
            else:
                await self._disconnect(entry, key)
        to_release: list[tuple[tuple[str, str], ParkedEntry]] = [
            (key, parked)
            for key in list(self.parked)
            if (parked := self.parked.get(key)) is not None and (terminate or not parked.transfers)
        ]
        for key, _p in to_release:
            self.parked.pop(key, None)
        # Concurrent: N parked entries release in parallel (each release's
        # own timeout is 5s; sequentially that could stall shutdown N×5s on
        # an unreachable remote server). Handover KEPT the transferable
        # HTTP entries — their ids travel in the payload for the successor.
        if to_release:
            await asyncio.gather(
                *(parked.release(self, key) for key, parked in to_release),
                return_exceptions=True,
            )
        await self._close_bases()
        # Cancel sweep-retry tasks (``expedite_park``'s _sweep_soon sleeps up
        # to ~3.5s — draining them at shutdown stalled handover and swept a
        # dying registry). They never need to run at shutdown.
        for task in list(self._expedite_tasks):
            task.cancel()
        # Drain pending deferred dead-child closes so shutdown WAITS for them
        # instead of cancelling: a close cancelled mid-flight could leave a
        # child that is only session-dead (process possibly alive). Bounded
        # (the prod closes are ~sub-ms; 5s is insurance against a hung close).
        # Sweeper cancel rides the finally: it runs even if close_all is
        # externally cancelled mid-drain.
        try:
            if self._dead_close_tasks:
                await asyncio.wait(self._dead_close_tasks, timeout=5)
        except Exception:
            logger.debug("isolated: dead-close drain interrupted", exc_info=True)
        finally:
            self._dead_close_tasks.clear()
            if self._sweeper is not None:
                self._sweeper.cancel()
                self._sweeper = None

    async def _disconnect(
        self, entry: LiveEntry, key: tuple[str, str], dropped_sessions: bool = False
    ) -> None:
        """Dispose of an entry's client per its transport kind — the ONE choke
        point for teardown correctness.

        stdio: ``close()`` — the transport's keep_alive would otherwise leave
        the SUBPROCESS running (reparented orphan). HTTP: the inherited
        ``_disconnect(force=True)``; with ``dropped_sessions`` the Resumable
        transport is told ``terminate_on_close=False`` first — the sessions
        died with the server (evict) or were already released (park) and must
        never be deleted (the Resumable close would otherwise send it).
        """
        transport = getattr(entry.client, "transport", None)
        try:
            if isinstance(transport, StdioTransport):
                await _close_client(entry.client)
            else:
                if dropped_sessions and isinstance(transport, ResumableStreamableHttpTransport):
                    transport.terminate_on_close = False  # session already dead
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
        self.bases.clear()
        self.grace = GRACE_SECONDS
        self.ttl = PARK_TTL_SECONDS
        if self._sweeper is not None:
            self._sweeper.cancel()
            self._sweeper = None
        for task in list(self._expedite_tasks):
            task.cancel()
        self._expedite_tasks.clear()
        for task in list(self._dead_close_tasks):
            task.cancel()
        self._dead_close_tasks.clear()


REGISTRY = IsolatedSessionRegistry()


class TokenKeyedStatefulClient(StatefulProxyClient[ClientTransportT]):
    """``StatefulProxyClient`` whose per-chat cache is keyed by chat token.

    Tokened requests resolve their upstream client through ``REGISTRY`` by
    ``(server, token)`` — surviving downstream session churn — and resume
    parked upstream sessions via a seeded transport. Tokenless requests fall
    through to the inherited per-``Connection`` cache with its
    disconnect-on-exit lifecycle.

    ``acquire_stateful`` (async, so the parked-resume probe can await) is the
    factory to hand to the proxy; ``new_stateful`` remains the sync
    tokenless fallback.
    """

    def __init__(self, *args: Any, server_name: str, **kwargs: Any) -> None:
        kwargs.setdefault("mode", CLIENT_CONNECT_MODE)
        super().__init__(*args, **kwargs)
        self._server_name = server_name

    async def acquire_stateful(self) -> Client[Any]:
        token = _request_group_key()
        if token is None:
            return super().new_stateful()

        session = _request_connection()

        entry = REGISTRY.acquire(self._server_name, token, session)
        if entry is not None:
            return entry.client

        parked = REGISTRY.pop_parked(self._server_name, token)
        if isinstance(parked, ParkedSession):
            client = await self._try_resume(parked)
            if client is not None:
                REGISTRY.register(self._server_name, token, client, session)
                logger.info(
                    "isolated: resumed upstream session for %s (token %s…)",
                    self._server_name,
                    token[:8],
                )
                return client
        elif parked is not None:
            # Wrong-kind parked entry for this server (unreachable by
            # construction — a server is one kind, and handover writes
            # ParkedSession only). Release loudly rather than vanish it.
            logger.warning(
                "isolated: wrong-kind parked entry for %s (token %s…) — releasing",
                self._server_name,
                token[:8],
            )
            await parked.release(REGISTRY, (self._server_name, token))

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
        if not isinstance(transport, ResumableStreamableHttpTransport):  # pragma: no cover
            # _new_per_chat copies the wrapper; it cannot change kind.
            raise RuntimeError("resumable transport did not survive the clone")
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


class TokenKeyedStdioClient(StatefulProxyClient[StdioTransport]):
    """Per-chat stdio SUBPROCESS isolation — the stdio analog of
    ``TokenKeyedStatefulClient``.

    A stdio subprocess IS the per-chat session: there are no transport-level
    session ids to park/resume, so the lifecycle collapses onto registry
    tiers that keep or terminate the child. Tokened requests resolve their
    upstream client through ``REGISTRY`` by ``(server, token)`` — the same
    subprocess for the same chat, a fresh one per new chat. Grace expiry
    leaves the child running (its in-memory state preserved — the direct
    analog of keeping an HTTP server-side session alive); ttl expiry
    terminates it; a token re-appearing inside the window reattaches to the
    still-running process.

    Tokenless requests fall through to the inherited per-``Connection``
    cache over the SHARED base subprocess — the documented
    supervised/unsupervised asymmetry (tokenless chats are never isolated).

    A child that crashes mid-chat is healed at the chat's next call: the
    acquire detects the dead child (the same liveness signal fastmcp's stdio
    transport uses) and respawns a fresh one — in-memory state is lost, only
    the call that was in flight when the child died errors. The chat needs no
    manual restart.
    """

    def __init__(
        self,
        *,
        server_name: str,
        command: str,
        args: list[str],
        env: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            StdioTransport(command=command, args=args, env=env), mode=CLIENT_CONNECT_MODE
        )
        self._server_name = server_name
        self._command = command
        self._args = args
        self._env = env
        # Track the tokenless-fallback BASE subprocess so evict/close_all can
        # close it too (keep_alive would otherwise orphan it).
        REGISTRY.register_base(server_name, self)

    async def acquire_stateful(self) -> Client[Any]:
        token = _request_group_key()
        if token is None:
            # Two tokenless shapes:
            #  (a) a tokenless chat in-request — the inherited per-Connection
            #      machinery keys the SHARED base subprocess;
            #  (b) OUT of any request context — the startup/prime tools/list.
            #      ``new_stateful`` needs a server request context and would
            #      raise, so serve the probe with an EPHEMERAL client
            #      (keep_alive=False): the proxy's list runs in one context
            #      and the child dies with it — startup never leaks children.
            in_request = True
            try:
                get_context().session
            except Exception:
                in_request = False
            if not in_request:
                return self._new_stdio_probe()
            return super().new_stateful()

        session = _request_connection()

        entry = REGISTRY.acquire(self._server_name, token, session)
        if entry is not None:
            if not self._stdio_child_dead(entry.client):
                return entry.client
            # The chat's child died (crash/SIGKILL): heal transparently. The
            # stateful clone's __aexit__ never reconnects, so without this the
            # registry would keep handing back the dead client forever. The
            # child IS the chat's session — respawn it with fresh state and
            # drop the dead entry.
            logger.warning(
                "isolated: stdio child for %s (token %s…) died — respawning fresh",
                self._server_name,
                token[:8],
            )
            # Order matters for the concurrent case (parallel tool calls in
            # one chat): drop the dead entry, spawn + register the fresh one
            # with NO await in between — a racing acquire then finds the live
            # fresh entry instead of spawning a second child (whose registry
            # entry the first one would overwrite, leaking it: unreachable by
            # evict AND close_all). Only THEN close the dead child.
            REGISTRY.live.pop((self._server_name, token), None)
            healed = self._new_stdio_per_chat()
            REGISTRY.register(self._server_name, token, healed, session)
            _defer_close_dead(entry.client, (self._server_name, token), REGISTRY)
            return healed

        proc = REGISTRY.pop_parked(self._server_name, token)
        if isinstance(proc, ParkedProc):
            if self._stdio_child_dead(proc.client):
                # The parked child died in its sleep — park/forget semantics
                # collapse to "spawn fresh" (there is nothing left to keep).
                # Same synchronous heal ordering as the live branch: spawn +
                # register BEFORE closing the dead child (a racing acquire for
                # this token finds the fresh entry, not a second spawn).
                logger.warning(
                    "isolated: parked stdio process for %s (token %s…) died — respawning fresh",
                    self._server_name,
                    token[:8],
                )
                healed = self._new_stdio_per_chat()
                REGISTRY.register(self._server_name, token, healed, session)
                _defer_close_dead(proc.client, (self._server_name, token), REGISTRY)
                return healed
            else:
                # Same chat again: reattach to its STILL-RUNNING subprocess.
                REGISTRY.register(self._server_name, token, proc.client, session)
                logger.info(
                    "isolated: reused stdio process for %s (token %s…)",
                    self._server_name,
                    token[:8],
                )
                return proc.client
        elif proc is not None:
            # Wrong-kind parked entry for this stdio server (unreachable by
            # construction) — release loudly, never silently vanish it.
            logger.warning(
                "isolated: wrong-kind parked entry for %s (token %s…) — releasing",
                self._server_name,
                token[:8],
            )
            await proc.release(REGISTRY, (self._server_name, token))

        client = self._new_stdio_per_chat()
        REGISTRY.register(self._server_name, token, client, session)
        logger.debug(
            "isolated: spawned stdio process for %s (token %s…)",
            self._server_name,
            token[:8],
        )
        return client

    def _stdio_child_dead(self, client: Client[Any]) -> bool:
        """Whether this chat's stdio child has died (crash/SIGKILL).

        The stateful clone's ``__aexit__`` does nothing, so its session task
        is still pending with no live child underneath; the transport knows:
        a DONE connect task, or dead session streams (a task that was never
        STARTED is treated as connectable — see the None branch). Uses
        private fastmcp APIs, read defensively — pinned by a test.
        """
        t = getattr(client, "transport", None)
        if not isinstance(t, StdioTransport):
            return False
        # Private fastmcp APIs — read defensively so a fastmcp minor bump that
        # renames them fails SOFT (treated alive; the next call retries) rather
        # than taking down every tokened chat acquire.
        connect_task = getattr(t, "_connect_task", None)
        if connect_task is None:
            # Never connected at all — NOT a crash: the machinery connects it
            # on first use. (A parked entry can be here when its chat detached
            # mid-connect.) Reattach is correct; connect will spawn the child.
            return False
        try:
            if connect_task.done():
                return True
        except AttributeError:
            return False
        is_session_dead = getattr(t, "_is_session_dead", None)
        if is_session_dead is None:
            return False
        return bool(is_session_dead())

    def _new_stdio_per_chat(self) -> Client[Any]:
        """A per-chat client that is a ``StatefulProxyClient`` clone with a
        PRIVATE ``StdioTransport``.

        Two reasons not to return a plain ``Client``: (1) the stateful
        machinery keeps the child CONNECTED across the proxy's per-call
        contexts — a plain client disconnects and re-initializes on every
        call, which a strict server rejects; (2) the clone carries fastmcp's
        proxy handlers (log/progress/sampling/roots forwarding) that the HTTP
        per-chat path gets through the same clone mechanism. Crash note: the
        acquire detects a dead child and respawns a fresh one — in-memory
        state is lost; only the call in flight when the child died fails.
        """
        client = self.new()  # StatefulProxyClient clone — handlers, config
        client.transport = StdioTransport(command=self._command, args=self._args, env=self._env)
        return client

    def _new_stdio_probe(self) -> Client[Any]:
        """An EPHEMERAL client for out-of-context tools/list (startup prime):
        ``keep_alive=False`` so the child dies with the proxy's list context
        instead of lingering for a chat that never calls us."""
        return Client(
            StdioTransport(command=self._command, args=self._args, env=self._env, keep_alive=False),
            mode=CLIENT_CONNECT_MODE,
        )


def _defer_close_dead(client: Client[Any], key: tuple[str, str], registry: Any) -> None:
    """Close a dead per-chat child in a STRONGLY-REFERENCED background task.

    The heal path must have NO await between spawning/registering the fresh
    client and returning it — an await there is where concurrent acquires
    raced and leaked an untracked child. The close is fire-and-forget-safe:
    tracked on the registry's dead-close set so it cannot be GC'd, and
    ``_disconnect`` already logs and suppresses its own failures internally.
    ``close_all`` drains the set before finishing, so shutdown waits for the
    closes rather than cancelling them.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover — heals run inside the server loop
        return
    task = loop.create_task(registry._disconnect(LiveEntry(client=client), key))
    registry._dead_close_tasks.add(task)
    task.add_done_callback(registry._dead_close_tasks.discard)
