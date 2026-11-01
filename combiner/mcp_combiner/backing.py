"""Backing-process lifecycle: the common frame + the per-kind registry.

Every backing kind gets a small manager specializing
:class:`BackingManagerBase`:

- :class:`~mcp_combiner.sharedserver.SharedServerManager` — owned processes:
  sharedserver spawns/refcounts/decrefs them, with a grace period.
- :class:`~mcp_combiner.externalservers.ExternalServerManager` — non-owned
  processes (``docker run`` & co.): launch, poll, stop-or-leave.
- :class:`PlainUrlProbeManager` — plain url-only servers: background
  reachability probe only, nobody owns their process.

The base owns what is common to every kind — the claiming frame
(start_all / ensure_started / ensure_stopped / restart / stop_all /
backing_state) and the shared backing-state vocabulary — so no caller and no
facade ever branches on *which kind* a server is. Each manager declares the
server-entry field that claims servers (:meth:`BackingManagerBase.claims`);
the facade walks its manager list in order and the first claimant wins, which
is the single place backing precedence (e.g. ``sharedServer`` over
``externalServer``) is decided.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from mcp_combiner.externalservers import ExternalServerManager

from mcp_combiner.config import CombinerConfig, Transport

logger = logging.getLogger(__name__)

# The backing-state vocabulary is shared by every kind and overlaid onto
# connection-level status by ``status._overlay_backing_state``, which is why
# each manager's state dict uses the same strings. Nothing gates on these —
# they are observational. Values:
#   "spawning"     — launch in flight
#   "spawn_failed" — launch errored or timed out; no process running
#   "probing"      — launched (or claimed attach-only); reachability poll in progress
#   "unreachable"  — reachability poll timed out
#   "reachable"    — answered HTTP (any status counts — see _poll_url)
#   "spawned"      — launch succeeded; server has no URL to probe
#   "stopped"      — we ran this kind's stop behaviour for it


class BackingProcessLike(Protocol):
    """The manager surface backing-process consumers need (meta-tools).

    ``BackingProcessManager`` (and its test fakes) satisfy it structurally, so
    meta-tools stay decoupled from which backing kinds exist in this build.
    """

    async def ensure_started(self, server_name: str) -> None: ...

    async def ensure_stopped(self, server_name: str) -> None: ...

    async def restart(self, server_name: str) -> bool: ...


class BackingManagerBase(ABC):
    """Common lifecycle frames for one kind of backing process.

    Subclasses provide the kind's *identity* (``label``), *claim*
    (:meth:`claims` — which servers belong to this kind, and the config entry
    describing them), and four behaviour hooks (:meth:`_launch_and_poll`,
    :meth:`_stop`, :meth:`_stop_hard`, :meth:`_shutdown_coros`) plus small
    tunables (:attr:`stop_timeout`, :attr:`drain_timeout`, :meth:`_poll_fn`,
    :meth:`_health_timeout`, :meth:`restartable`). Everything else — the
    start/stop/restart/status flow, deduplication of multiple server entries
    referencing one backing entry, concurrent gather, and the backing-state
    bookkeeping — is the template below and is identical for every kind.
    """

    #: Log label for this kind, e.g. "sharedserver-managed".
    label: str = "backing"

    #: seconds granted to a stop on the non-shutdown paths (disable/restart).
    #: Shutdown uses :attr:`drain_timeout` — tighter, because the combiner
    #: itself races sharedserver's ~5s SIGTERM→SIGKILL window there.
    stop_timeout: float = 5.0
    drain_timeout: float = 2.0

    def __init__(self, config: CombinerConfig) -> None:
        self._config = config
        # Entry names this manager launched or is actively probing, in start
        # order. Tests inspect this (sharedserver's bookkeeping is a refcount
        # list; the external kind merely tracks what it launched).
        self._active: list[str] = []
        # Entries whose launch is currently in flight. Guards a doubled
        # ensure_started/restart from double-launching — a refcount bump is
        # harmless, but `docker run --name x` twice is a hard failure.
        self._in_flight: set[str] = set()
        # Disable requests that arrived while an entry's launch was in flight,
        # honoured the moment the launch completes (a detached launch can take
        # up to 30s — the backend must not end up running behind a "disabled"
        # server).
        self._stop_after_launch: set[str] = set()
        # Observed lifecycle per backing entry (see the state vocabulary
        # documented at module top). Overlay candidate for server status.
        self._backing_state: dict[str, str] = {}

    # ── kind identity (subclass hooks) ─────────────────────────────

    @abstractmethod
    def claims(self, server_name: str) -> Any | None:
        """The backing entry *server_name* belongs to this kind, else ``None``.

        Pure identity: field presence plus a resolvable reference. A dangling
        section reference claims nothing (it is warned about at load time), so
        status and lifecycle paths never see the raise. Cross-kind precedence
        (e.g. sharedServer over externalServer) does NOT live here — the
        facade's manager order owns it.
        """

    def restartable(self, entry: Any) -> bool:
        """Whether a hard stop+relaunch makes sense for *entry*."""
        return True

    def _poll_fn(self) -> Any:
        """The URL poll callable, resolved at call time so tests patching a
        manager module's ``_poll_url`` are honoured."""
        raise NotImplementedError

    def _health_timeout(self, entry: Any) -> int:
        """Seconds to poll the entry's server URL for."""
        return getattr(entry, "health_timeout", 5)

    async def _launch_and_poll(self, server_name: str, entry: Any, url: str | None) -> None:
        """Kind-specific launch, ending in the shared :meth:`_poll`."""
        raise NotImplementedError

    async def _stop(self, entry: Any, timeout: float) -> None:
        """Kind-specific stop behaviour (no state bookkeeping — the base adds it)."""
        raise NotImplementedError

    async def _stop_hard(self, entry: Any) -> None:
        """Hard destruction of the backing process, for :meth:`restart`."""
        await self._stop(entry, self.stop_timeout)

    @abstractmethod
    def _shutdown_coros(self) -> list[Any]:
        """One stop coroutine per started entry name, for :meth:`stop_all`.

        Built *synchronously* (coroutines returned unawaited) so a kind whose
        tooling is missing raises ``FileNotFoundError`` HERE —
        :meth:`stop_all` then gives up without clearing ``_active`` (its
        references stay held and remain retryable).
        """

    # ── shared lifecycle frames ────────────────────────────────────

    def backing_state(self, server_name: str) -> str | None:
        """Observed state of *server_name*'s backing process, or ``None``."""
        entry = self.claims(server_name)
        return self._backing_state.get(entry.name) if entry is not None else None

    async def start_all(self, names: Sequence[str] | None = None) -> None:
        """Launch every enabled server this kind claims, concurrently.

        ``names`` scopes the launch to the given servers: the facade passes
        each manager exactly the servers that *chose* it (claimant order), so
        cross-kind precedence applies at launch time and not just at dispatch.
        Standalone callers (and tests) pass nothing and enumerate their own
        claims. Multiple server entries may reference one backing entry —
        launched once, deduped by entry name. Failures are logged; the
        combiner mounts the rest either way.
        """
        server_names = (
            list(names) if names is not None else list(self._config.get_enabled_servers())
        )
        refs: list[tuple[str, Any, str | None]] = []
        seen: set[str] = set()
        for name in server_names:
            srv = self._config.servers.get(name)
            if srv is None or srv.disabled:
                continue
            entry = self.claims(name)
            if entry is None or entry.name in seen or entry.name in self._active:
                # ``_active`` skip: start_all is boot-only today, but a second
                # call (reload growth, future callers) must never re-run a
                # launcher for something we already started.
                continue
            seen.add(entry.name)
            refs.append((name, entry, srv.url))
        if refs:
            logger.warning("Starting %s servers: %s", self.label, ", ".join(n for n, _, _ in refs))
            results = await asyncio.gather(
                *(self._launch(name, entry, url) for name, entry, url in refs),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, BaseException):
                    logger.warning("%s start failed: %s", self.label, result)
        else:
            logger.warning("No %s backing configured", self.label)

    async def _launch(self, server_name: str, entry: Any, url: str | None) -> None:
        """Launch under the double-launch guard (see ``_in_flight``), honouring
        a disable that raced the launch."""
        if entry.name in self._in_flight:
            return
        self._in_flight.add(entry.name)
        try:
            await self._launch_and_poll(server_name, entry, url)
        finally:
            self._in_flight.discard(entry.name)
            await self._maybe_stop_after_launch(entry)

    async def _maybe_stop_after_launch(self, entry: Any) -> None:
        """Honour a disable that arrived while the entry was launching.

        The launch has completed by now — if a ``stop`` was requested
        meanwhile (``BackingManagerBase.ensure_stopped`` raced an in-flight
        launch), stop the freshly-launched process immediately instead of
        leaving a running backend behind a "disabled" server.
        """
        if entry.name not in self._stop_after_launch:
            return
        self._stop_after_launch.discard(entry.name)
        if entry.name not in self._active:
            return  # launch failed — nothing running to stop
        try:
            await self._stop(entry, self.stop_timeout)
        except FileNotFoundError:
            return
        self._backing_state[entry.name] = "stopped"
        self._active.remove(entry.name)

    async def ensure_started(self, server_name: str) -> None:
        """Start *server_name*'s backing process if this kind claims it.

        Idempotent: no-op for unclaimed servers, entries already started, or
        launches currently in flight. Called at boot and on dynamic enable.
        """
        entry = self.claims(server_name)
        if entry is None:
            return
        if entry.name in self._in_flight:
            # Enable racing an in-flight launch/restart: the LAST request
            # wins — cancel any stop-after-launch intent (a disable that
            # raced the 30s launch must not stop the fresh backend once it
            # completes). The launch itself is already the "start" being
            # requested.
            self._stop_after_launch.discard(entry.name)
            return
        if entry.name in self._active:
            return
        srv = self._config.servers.get(server_name)
        await self._launch(server_name, entry, srv.url if srv else None)

    async def ensure_stopped(self, server_name: str) -> None:
        """Stop *server_name*'s backing process, if we started it. Idempotent.

        Runs the kind's :meth:`_stop` (which is whatever "stopping" means for
        that kind — a decref as well as a stop, for sharedserver). A lost
        binary (``FileNotFoundError``) leaves state untouched so a later
        unuse/stop can still be attempted.
        """
        entry = self.claims(server_name)
        if entry is None:
            return
        if entry.name in self._in_flight:
            # Disable raced an in-flight launch/restart: honour it as soon as
            # the launch completes instead of stopping mid-restart (or never).
            self._stop_after_launch.add(entry.name)
            return
        if entry.name not in self._active:
            return
        try:
            await self._stop(entry, self.stop_timeout)
        except FileNotFoundError:
            return
        self._backing_state[entry.name] = "stopped"
        self._active.remove(entry.name)

    async def restart(self, server_name: str) -> bool:
        """Hard-restart *server_name*'s backing process for *server_name*.

        ``True`` = a process of ours was destroyed and relaunched (the caller
        then remounts); ``False`` = nothing to hard-restart here — either
        unclaimed, or the entry is not restartable (attach-only entries have
        no process of ours; the caller falls back to its reconnect path).
        """
        entry = self.claims(server_name)
        if entry is None or not self.restartable(entry):
            return False
        if entry.name in self._in_flight:
            return False  # launch in progress — restart would race it
        self._in_flight.add(entry.name)
        try:
            try:
                await self._stop_hard(entry)
            except FileNotFoundError as exc:
                logger.warning("Cannot restart '%s': %s", server_name, exc)
                return False
            except Exception:
                logger.exception("restart: stop failed for '%s' — respawning anyway", server_name)
            # Drop the pre-restart bookkeeping before relaunch: the launch
            # path appends, and a stale duplicate would make a later disable
            # a no-op ("already active") and over-unuse/over-stop the entry.
            if entry.name in self._active:
                self._active.remove(entry.name)
            if entry.name in self._stop_after_launch:
                # A disable raced the restart — the LAST request wins: do NOT
                # relaunch. The stop-hard above already destroyed the old
                # process; record it stopped and let the caller reconnect or
                # re-enable later. (Meta-tools then skip the remount.)
                self._stop_after_launch.discard(entry.name)
                self._backing_state[entry.name] = "stopped"
                return False
            srv = self._config.servers.get(server_name)
            await self._launch_and_poll(server_name, entry, srv.url if srv else None)
            return True
        finally:
            self._in_flight.discard(entry.name)
            await self._maybe_stop_after_launch(entry)

    async def stop_all(self) -> None:
        """Shut everything we started down — concurrently, on the short
        drain bound. Runs from ASGI shutdown where the combiner itself races
        sharedserver's ~5s SIGTERM→SIGKILL escalation, hence
        :attr:`drain_timeout` rather than :attr:`stop_timeout`.
        """
        if not self._active:
            return
        names = list(self._active)
        try:
            coros = self._shutdown_coros()
        except FileNotFoundError:
            return  # kind's binary is gone; keep the references (retryable)
        await asyncio.gather(*coros, return_exceptions=True)
        self._active.clear()
        for name in names:
            self._backing_state[name] = "stopped"

    # ── shared internals ───────────────────────────────────────────

    async def _poll(self, server_name: str, entry: Any, url: str | None) -> None:
        """Wait up to the entry's health timeout for the server URL to answer."""
        if not url:
            self._backing_state[entry.name] = "spawned"
            return
        self._backing_state[entry.name] = "probing"
        health_url = url.rstrip("/")
        logger.info(
            "Waiting up to %ds for '%s' at %s",
            self._health_timeout(entry),
            server_name,
            health_url,
        )
        ready = await self._poll_fn()(health_url, self._health_timeout(entry))
        if ready:
            self._backing_state[entry.name] = "reachable"
            logger.info("'%s' is healthy", server_name)
        else:
            self._backing_state[entry.name] = "unreachable"
            logger.warning(
                "'%s' did not become healthy within %ds — "
                "proxy will be mounted but may fail until server is ready",
                server_name,
                self._health_timeout(entry),
            )


class PlainUrlProbeManager(BackingManagerBase):
    """Claim everything no other kind manages, and just probe it.

    Plain url-only servers (http/sse, no backing reference) get the same
    reachability verdict as backed entries for status parity — but the probe
    runs as a *background* task (:meth:`_launch_and_poll` returns
    immediately) so a down remote server can't delay combiner readiness the
    way a launch poll may. There is no process to start, stop, or restart;
    ``restart`` is always ``False``.

    Probes are re-run on every (re-)enable: a disable cancels the task and
    records ``stopped``, and the next ``ensure_started`` launches a fresh
    background verdict rather than letting the stale one linger.
    """

    label = "url-probed"
    stop_timeout = 0.0
    drain_timeout = 0.0

    def __init__(self, config: CombinerConfig, others: list[BackingManagerBase]) -> None:
        super().__init__(config)
        # Sibling managers, in facade precedence order: a server any of them
        # claims is backed, not probed.
        self._others = others
        # Background probe tasks, keyed by server name.
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def claims(self, server_name: str) -> Any | None:
        srv = self._config.servers.get(server_name)
        if srv is None or not srv.url or srv.transport is Transport.STDIO:
            return None
        if any(other.claims(server_name) is not None for other in self._others):
            return None
        return srv  # the probe's "entry" is the server config itself

    def restartable(self, entry: Any) -> bool:
        return False

    def _poll_fn(self) -> Any:
        from mcp_combiner import sharedserver as _sharedserver  # lazy: cycle-free

        return _sharedserver._poll_url

    def _health_timeout(self, entry: Any) -> int:
        return PLAIN_HEALTH_TIMEOUT

    async def _launch_and_poll(self, server_name: str, entry: Any, url: str | None) -> None:
        """Spawn the background probe for *server_name* and return at once.

        The base frames call this for both boot (``start_all``) and dynamic
        (re-)enable — one path, always non-blocking. A probe already running
        for the server is left alone (the base's ``_active`` guard prevents
        the double)."""
        if url is None:
            self._backing_state[server_name] = "spawned"
            return
        existing = self._tasks.get(server_name)
        if existing is not None and not existing.done():
            return  # probe already in flight
        self._active.append(server_name)
        task = asyncio.ensure_future(self._poll(server_name, entry, url))
        self._tasks[server_name] = task

        def _drop(_task: asyncio.Task[None], _name: str = server_name) -> None:
            # Remove only OUR task: a disable→enable race can have stored a
            # newer probe under the same name by the time this fires — popping
            # by name would orphan it (undoable cancel, invisible to stop_all).
            if self._tasks.get(_name) is _task:
                del self._tasks[_name]
            # Fire-and-forget tasks must have their exceptions retrieved —
            # otherwise asyncio logs "Task exception was never retrieved".
            if not _task.cancelled() and _task.exception() is not None:
                logger.warning("url probe for '%s' failed: %s", _name, _task.exception())

        task.add_done_callback(_drop)

    async def _stop(self, entry: Any, timeout: float) -> None:
        task = self._tasks.pop(entry.name, None)
        if task is not None:
            task.cancel()

    async def stop_all(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        self._tasks.clear()
        # Match the base contract (base.stop_all clears it after gathering);
        # this override runs only at shutdown, where states are moot.
        self._active.clear()

    def _shutdown_coros(self) -> list[Any]:
        # Probe lifecycle is cancel-only; stop_all is overridden and never
        # reaches here. Satisfies the abstract hook with a no-op.
        return []


# Poll budget for plain url-only servers. Background — never delays boot.
PLAIN_HEALTH_TIMEOUT = 30


class BackingProcessManager:
    """One handle for every backing kind — the registry facade.

    Callers (lifespan, meta-tools, status overlay, connection monitor) hold
    this and never branch on which section backs which server. Precedence is
    the manager *order*: the first manager claiming a server wins, which is
    where "sharedServer over externalServer" is decided (a single decision,
    enforced once — the load-time warning in ``CombinerConfig.load`` echoes
    it for the config author). Servers nobody claims fall through: they are
    plain url-only, and the trailing probe manager claims them.
    """

    def __init__(
        self, config: CombinerConfig, managers: list[BackingManagerBase] | None = None
    ) -> None:
        # Lazy imports: the manager modules import this module (the base
        # lives here), so resolving them at module import time would cycle.
        from mcp_combiner.externalservers import ExternalServerManager
        from mcp_combiner.sharedserver import SharedServerManager

        built = managers
        if built is None:
            shared = SharedServerManager(config)
            external = ExternalServerManager(config)
            built = [shared, external, PlainUrlProbeManager(config, [shared, external])]
        self._managers: list[BackingManagerBase] = built
        self._config = config
        # Typed named handles for direct use/tests; order still owns
        # precedence. Matched by kind so an injected custom manager list still
        # yields ``None`` rather than a wrong-typed handle.
        self.shared: BackingManagerBase = built[0]
        self.external: ExternalServerManager | None = next(
            (m for m in built if isinstance(m, ExternalServerManager)), None
        )
        self.probe: PlainUrlProbeManager | None = next(
            (m for m in built if isinstance(m, PlainUrlProbeManager)), None
        )

    # ── registry dispatch (no kind switches) ───────────────────────

    def claimant(self, server_name: str) -> BackingManagerBase | None:
        """The first manager (in precedence order) claiming *server_name*."""
        for manager in self._managers:
            if manager.claims(server_name) is not None:
                return manager
        return None

    def backing_state(self, server_name: str) -> str | None:
        manager = self.claimant(server_name)
        return manager.backing_state(server_name) if manager else None

    async def start_all(self) -> None:
        """Launch per kind through ``claimant()`` — the same precedence that
        governs every other dispatch applies at boot: a server setting both
        references launches ONE backend, not two (and the trailing probe gets
        exactly the plain url-only servers)."""
        groups: dict[BackingManagerBase, list[str]] = {}
        for name, _srv in self._config.get_enabled_servers().items():
            manager = self.claimant(name)
            if manager is None:
                continue
            groups.setdefault(manager, []).append(name)
        results = await asyncio.gather(
            *(manager.start_all(names) for manager, names in groups.items()),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                logger.warning("backing process start failed: %s", result)

    async def ensure_started(self, server_name: str) -> None:
        manager = self.claimant(server_name)
        if manager is not None:
            await manager.ensure_started(server_name)

    async def ensure_stopped(self, server_name: str) -> None:
        manager = self.claimant(server_name)
        if manager is not None:
            await manager.ensure_stopped(server_name)

    async def restart(self, server_name: str) -> bool:
        """Hard-restart via the claiming manager; ``False`` when no manager
        claims the server (plain url-only — the caller reconnects instead)."""
        manager = self.claimant(server_name)
        return await manager.restart(server_name) if manager else False

    async def stop_all(self) -> None:
        await asyncio.gather(
            *(manager.stop_all() for manager in self._managers), return_exceptions=True
        )
