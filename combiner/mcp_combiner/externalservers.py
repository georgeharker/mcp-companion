"""Non-owned backing processes (``externalServers`` config section).

``SharedServerConfig`` (sharedserver.py) is for processes the combiner *owns*:
sharedserver spawns them, refcounts clients, and keeps them warm through a
grace period. This module covers the other case — a backing process whose
lifetime some other tool decides (``docker run``, ``docker compose up -d``),
where the combiner's job is narrower: launch (maybe), wait for the URL, and
on shutdown do exactly what the config says. No refcount, no grace period —
a detached container has exactly one lifetime, and it belongs to its launcher.

Every lifecycle *frame* (start_all / ensure_started / ensure_stopped /
restart / stop_all / backing_state) is inherited from
:class:`~mcp_combiner.backing.BackingManagerBase`; only the claim and four
behaviour hooks below are external-shaped. Cross-kind dispatch and precedence
live in :class:`~mcp_combiner.backing.BackingProcessManager` (the registry
facade), which also owns the plain url-only background probe.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from mcp_combiner.backing import BackingManagerBase
from mcp_combiner.config import (
    CombinerConfig,
    ExternalServerConfig,
    _interpolate_dict,
    _interpolate_list,
)
from mcp_combiner.sharedserver import _poll_url

logger = logging.getLogger(__name__)

# How long a *detached* launcher (`docker run -d` — exits immediately after
# asking the daemon to start the container) is allowed to hang around before
# we declare the launch failed. Generous: image pulls can be slow.
DETACHED_LAUNCH_TIMEOUT = 30.0

# How long a `stop` command may run on the non-shutdown paths (disable /
# restart). `docker stop` can take ~10s (SIGTERM, then SIGKILL), so this is
# not tight — the tight bound lives in stop_all (DRAIN_TIMEOUT) where the
# combiner itself is racing sharedserver's ~5s SIGTERM→SIGKILL escalation.
STOP_TIMEOUT = 10.0

# Per-stop deadline in stop_all — fits the ~5s window sharedserver gives the
# combiner on shutdown (SIGTERM→SIGKILL), same rationale as the 2s `unuse`
# bound in SharedServerManager._stop_one. A killed `docker stop` CLI still
# leaves the stop request in flight at the daemon, so the container stops.
DRAIN_TIMEOUT = 3.0

# Foreground children get this long to react to SIGTERM before SIGKILL.
TERMINATE_TIMEOUT = 5.0


async def _drain_pipe(name: str, pipe: asyncio.StreamReader | None, level: int) -> None:
    """Pump ONE foreground child pipe into the combiner log until EOF.

    One task per pipe — never a sequential reader over both: reading stdout
    to EOF before touching stderr deadlocks once a chatty child fills the
    stderr pipe buffer (the child blocks writing stderr, so stdout never
    EOFs). Cancelled at shutdown alongside the child's termination.

    Reads in bounded CHUNKS and splits lines itself — ``async for line``
    (StreamReader.readline) raises ``ValueError`` once a single line exceeds
    the 64KiB limit, which would kill the drain task and wedge the child on
    a full pipe (docker often emits very large JSON log lines).
    """
    if pipe is None:
        return
    buf = b""
    while True:
        chunk = await pipe.read(65536)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, _, buf = buf.partition(b"\n")
            text = line.decode(errors="replace").rstrip()
            if text:
                logger.log(level, "externalServer '%s': %s", name, text)
    text = buf.decode(errors="replace").rstrip()
    if text:
        logger.log(level, "externalServer '%s': %s", name, text)


class ExternalServerManager(BackingManagerBase):
    """Launches (and optionally stops) each ``externalServers`` backing process.

    The external kind's specialization: launch argv interpolation, the
    foreground-child bookkeeping (pipeline draining + SIGTERM at shutdown),
    detached-launch exit checking, and the stop-or-leave-running semantics.
    ``_active`` tracks entries *this combiner* launched or is probing, but
    there is nothing to decref — stopping runs the configured ``stop`` (or
    terminates a foreground child) and that's all; a detached launcher with
    no ``stop`` configured stays running by design. Attach-only entries have
    no process of ours, so ``restartable`` is ``False`` for them (restart
    falls back to the caller's plain reconnect path).

    Backing states are the shared vocabulary (documented on
    :class:`~mcp_combiner.backing.BackingManagerBase`), so the status overlay
    is shared verbatim.
    """

    label = "external-backed"
    stop_timeout = STOP_TIMEOUT
    drain_timeout = DRAIN_TIMEOUT

    def __init__(self, config: CombinerConfig) -> None:
        super().__init__(config)
        # Foreground children (``foreground: true``), keyed by entry name.
        # Kept so shutdown can SIGTERM the process the combiner owns.
        self._children: dict[str, asyncio.subprocess.Process] = {}
        # Pipe-drain tasks for foreground children (prevent pipe-buffer stalls
        # and keep docker's stdout/stderr visible in the combiner log).
        self._drain_tasks: dict[str, list[asyncio.Task[None]]] = {}
        # Entries as they were when we launched them — shutdown stops the
        # process the config *asked for at launch time*, not whatever the
        # config says after a reload dropped or rewrote the entry.
        self._entry_snapshots: dict[str, ExternalServerConfig] = {}

    # ── kind identity ──────────────────────────────────────────────

    def claims(self, server_name: str) -> ExternalServerConfig | None:
        """This server's ``externalServer`` reference, if it has one.

        A dangling reference (missing ``externalServers`` entry — warned at
        load time) claims nothing, so status/lifecycle paths never see the
        ``KeyError`` ``resolve_external_server`` raises for it.
        """
        try:
            return self._config.resolve_external_server(server_name)
        except KeyError:
            return None

    def restartable(self, entry: ExternalServerConfig) -> bool:
        # Attach-only: nothing was launched, so there is no process to restart.
        # A detached launch with no ``stop`` can't be restarted safely either:
        # re-running the launch while the old process lives would fail
        # (``docker run -d --name x`` — name taken) and worse, the restart
        # would not honour the config's "leave it running" posture.
        return entry.launch is not None and (entry.stop is not None or entry.foreground)

    def _poll_fn(self) -> Any:
        # Module global resolved at call time — tests patch
        # ``mcp_combiner.externalservers._poll_url``.
        return _poll_url

    # ── behaviour hooks ────────────────────────────────────────────

    async def _launch_and_poll(
        self,
        server_name: str,
        entry: ExternalServerConfig,
        url: str | None,
    ) -> None:
        ext = entry
        if ext.launch is None:
            # Attach-only: poll the (possibly already-running) server's URL;
            # there is no process of ours to launch.
            self._active.append(ext.name)
            await self._poll(server_name, ext, url)
            return

        # Log the argv BEFORE interpolation: launch lines commonly embed
        # `${env:TOKEN}`-style references, and interpolated values are
        # plaintext secrets in the log.
        logger.warning(
            "Starting externalServer '%s' via: %s",
            ext.name,
            " ".join(ext.launch),
        )
        self._entry_snapshots[ext.name] = ext
        cmd = _interpolate_list(ext.launch)
        self._backing_state[ext.name] = "spawning"

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                env=self._merge_env(ext),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE if ext.foreground else asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError) as exc:
            logger.warning("externalServer launch '%s' failed: %s", ext.name, exc)
            self._backing_state[ext.name] = "spawn_failed"
            return

        if ext.foreground:
            # We own this child: keep the handle for shutdown, and drain each
            # pipe in its own task so a chatty child can't wedge on a full
            # buffer and a fast-piping child can't deadlock stdout-vs-stderr
            # (we never communicate() them to completion here).
            self._children[ext.name] = proc
            self._drain_tasks[ext.name] = [
                asyncio.ensure_future(_drain_pipe(ext.name, proc.stdout, logging.INFO)),
                asyncio.ensure_future(_drain_pipe(ext.name, proc.stderr, logging.WARNING)),
            ]
        else:
            # Detached launch: a fire-and-forget CLI call whose exit code and
            # stderr are the only failure signals we get. Bounded by
            # DETACHED_LAUNCH_TIMEOUT (image pulls can be slow); a hung
            # launcher is killed rather than leaked.
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), DETACHED_LAUNCH_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning("externalServer launch '%s' timed out — killing it", ext.name)
                proc.kill()
                await proc.wait()
                self._backing_state[ext.name] = "spawn_failed"
                return
            if proc.returncode != 0:
                stderr_tail = stderr.decode(errors="replace").strip()[-500:]
                logger.warning(
                    "externalServer launch '%s' exited %d%s — is the launcher "
                    "idempotent for combiner restarts? (e.g. `docker run -d "
                    "--name x` fails when the container already exists)",
                    ext.name,
                    proc.returncode,
                    f": {stderr_tail}" if stderr_tail else "",
                )
                self._backing_state[ext.name] = "spawn_failed"
                return

        self._active.append(ext.name)
        logger.info("externalServer '%s' launched", ext.name)
        await self._poll(server_name, ext, url)

    async def _stop(self, entry: ExternalServerConfig, timeout: float) -> None:
        """Run the configured stop behaviour for *entry*.

        Order: ``stop`` command (if a process of ours was actually launched —
        attach-only entries never own one, so their ``stop`` never runs; the
        config loader warns about that combination) runs first, sharing the
        ``timeout`` budget with the child termination that follows; then a
        still-alive foreground child is SIGTERMed as a straggler fallback (it
        exits by itself once e.g. ``docker stop`` takes the container down).
        Nothing to do otherwise: a detached launcher with no ``stop`` config
        stays running by design. State bookkeeping (``stopped`` + ``_active``
        removal) is the base's.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        def _remaining() -> float:
            # The two stop phases SHARE the budget: drain-mode stops must fit
            # sharedserver's ~5s SIGTERM→SIGKILL window in total, not 3s + 5s.
            return max(0.25, deadline - loop.time())

        if entry.launch is not None and entry.stop is not None:
            cmd = _interpolate_list(entry.stop)
            logger.warning("Stopping externalServer '%s' via: %s", entry.name, " ".join(entry.stop))
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    env=self._merge_env(entry),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    _, stderr = await asyncio.wait_for(proc.communicate(), _remaining())
                    if proc.returncode != 0:
                        stderr_tail = stderr.decode(errors="replace").strip()[-300:]
                        logger.info(
                            "externalServer stop '%s' exited %s (continuing): %s",
                            entry.name,
                            proc.returncode,
                            stderr_tail or "(no diagnostics)",
                        )
                except asyncio.TimeoutError:
                    logger.warning("externalServer stop '%s' timed out", entry.name)
                    proc.kill()
                    # A killed stop command must be reaped, or we leak here
                    # exactly the way the child path below is careful not to.
                    try:
                        await proc.wait()
                    except (OSError, asyncio.CancelledError) as exc:  # pragma: no cover
                        logger.debug("stop reaping skipped for '%s': %s", entry.name, exc)
            except (OSError, ValueError) as exc:
                logger.warning("externalServer stop '%s' failed: %s", entry.name, exc)

        child = self._children.pop(entry.name, None)
        if child is not None and child.returncode is None:
            try:
                child.terminate()
                await asyncio.wait_for(child.wait(), _remaining())
            except asyncio.TimeoutError:
                logger.warning("externalServer child '%s' ignored SIGTERM — killing", entry.name)
                child.kill()
            except ProcessLookupError:
                logger.debug("externalServer child '%s' already gone", entry.name)
            # Reap either outcome — a kill() without wait() leaves a zombie.
            if child.returncode is None:
                try:
                    await child.wait()
                except Exception as exc:  # pragma: no cover — reaped concurrently
                    logger.debug(
                        "externalServer child reaping for '%s' skipped: %s", entry.name, exc
                    )
            logger.info("externalServer '%s' foreground child terminated", entry.name)
        for task in self._drain_tasks.pop(entry.name, []):
            task.cancel()

    def _shutdown_coros(self) -> list[Any]:
        """Shutdown-bound ``_stop`` per started entry (see base ``stop_all``).

        Uses the entry snapshot taken at launch: a reload that dropped or
        rewrote the entry must not change what a running process gets told at
        shutdown — ``docker stop <name>`` from launch time stays truthful.
        """
        coros: list[Any] = []
        for name in list(self._active):
            ext = self._entry_snapshots.get(name) or self._config.external_servers.get(name)
            if ext is None:
                logger.warning(
                    "externalServer '%s': entry no longer in config — skipping its stop", name
                )
                continue
            coros.append(self._stop(ext, self.drain_timeout))
        return coros

    # ── internals ──────────────────────────────────────────────────

    @staticmethod
    def _merge_env(ext: ExternalServerConfig) -> dict[str, str]:
        """Combiner environment overlaid with the entry's interpolated ``env``."""
        return {**os.environ, **_interpolate_dict(ext.env)}
