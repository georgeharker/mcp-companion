"""sharedserver integration for mcp-combiner.

Provides helpers to start and stop HTTP server processes via the ``sharedserver``
CLI tool, and to poll a URL until the server is reachable.

sharedserver owns the process lifetime — the combiner only increments/decrements
the reference count.  Multiple clients (Neovim instances, combiner processes) can
attach to the same named server concurrently; it stays alive as long as any
client holds a reference.

Typical flow::

    ss = SharedServerManager(config, sharedserver_bin="sharedserver")
    await ss.start_all()        # use + health-poll each server with sharedserver config
    ...
    await ss.stop_all()         # unuse all servers that were started
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from typing import TYPE_CHECKING, Any

from mcp_combiner.backing import BackingManagerBase

if TYPE_CHECKING:
    from mcp_combiner.backing import BackingProcessManager
    from mcp_combiner.config import CombinerConfig, SharedServerConfig

logger = logging.getLogger("mcp-combiner.sharedserver")


def _require_binary() -> str:
    """Return the path to ``sharedserver``, raising if not on PATH."""
    found = shutil.which("sharedserver")
    if not found:
        raise FileNotFoundError(
            "sharedserver not found on PATH. "
            "Install it with `cargo install sharedserver` or add it to your PATH."
        )
    return found


async def _poll_url(url: str, timeout: int) -> bool:
    """Poll *url* with HTTP GET until a response is received or *timeout* expires.

    Returns ``True`` if the server responded (any HTTP status), ``False`` on timeout.
    Uses asyncio subprocess to run ``curl --silent --max-time 1`` in a loop so we
    don't need an additional Python HTTP dependency at this layer.
    """
    import time

    deadline = time.monotonic() + timeout
    interval = 0.5

    while time.monotonic() < deadline:
        try:
            proc = await asyncio.create_subprocess_exec(
                "curl",
                # -q FIRST: ignore ~/.curlrc — the probe must be credential-free
                # and immune to a user's curlrc adding headers/proxies.
                "-q",
                "--silent",
                "--max-time",
                "1",
                "--output",
                "/dev/null",
                "--write-out",
                "%{http_code}",
                url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()  # reap — a killed curl is still a child
                continue
            if proc.returncode == 0 or (stdout and stdout.strip() not in (b"000", b"")):
                return True
        except (asyncio.TimeoutError, OSError):
            pass
        await asyncio.sleep(interval)

    return False


def _build_use_cmd(
    binary: str,
    ss: "SharedServerConfig",
    *,
    interpolate: bool = True,
    pid: int | None = None,
) -> list[str]:
    """Build the ``sharedserver use`` argv list.

    ``pid`` should be the long-lived process that owns the sharedserver
    reference (e.g. the combiner process).  sharedserver tracks this PID and
    decrements the refcount when it exits.  Without an explicit ``--pid`` the
    tool defaults to the *caller's* PID, which for async subprocess calls is
    the same as the combiner process — but we pass it explicitly for clarity.
    """
    from mcp_combiner.config import _interpolate_dict, _interpolate_list, _interpolate_str

    cmd = [binary, "use", ss.name]

    if ss.grace_period:
        cmd += ["--grace-period", ss.grace_period]

    # Tie the sharedserver reference to the combiner process explicitly.
    cmd += ["--pid", str(pid if pid is not None else os.getpid())]

    # Expand env vars in the process environment entries
    env_dict = _interpolate_dict(ss.env) if interpolate else ss.env
    for key, value in env_dict.items():
        cmd += ["--env", f"{key}={value}"]

    # Separator between sharedserver flags and the server command
    cmd.append("--")
    cmd.append(_interpolate_str(ss.command) if interpolate else ss.command)
    cmd += _interpolate_list(ss.args) if interpolate else ss.args

    return cmd


class SharedServerManager(BackingManagerBase):
    """Manages sharedserver lifecycle for all servers in a ``CombinerConfig``.

    The sharedserver kind's specialization is deliberately narrow: a
    ``use``-cmd launch with refcount bookkeeping (``_active`` is the list of
    references *we* hold), an ``unuse`` decref as the stop, and a hard
    ``admin stop --force`` for restarts. Every lifecycle frame
    (``backing_state``/``start_all``/``ensure_started``/``ensure_stopped``/
    ``restart``/``stop_all``) is inherited from
    :class:`~mcp_combiner.backing.BackingManagerBase` — only the claim and
    the four behaviour hooks below are sharedserver-shaped. Cross-kind
    dispatch/precedence lives in ``BackingProcessManager``, not here.

    Usage::

        mgr = SharedServerManager(config)
        await mgr.start_all()
        # ... combiner runs ...
        await mgr.stop_all()
    """

    label = "sharedserver-managed"
    # `unuse` is just a refcount decrement and should return fast. Bounded
    # short so a concurrent stop_all() completes well inside the ~5s
    # SIGTERM→SIGKILL window sharedserver gives us on shutdown.
    stop_timeout = 2.0
    drain_timeout = 2.0

    def __init__(self, config: "CombinerConfig") -> None:
        super().__init__(config)
        self._binary: str | None = None

    def _get_binary(self) -> str:
        if self._binary is None:
            self._binary = _require_binary()
        return self._binary

    def claims(self, server_name: str) -> "SharedServerConfig | None":
        """Resolve this server's ``sharedServer`` reference, if it has one.

        A dangling reference (missing ``sharedServers`` entry — warned at load
        time) claims nothing, so status/lifecycle paths never see the
        ``KeyError`` ``resolve_shared_server`` raises for it.
        """
        try:
            return self._config.resolve_shared_server(server_name)
        except KeyError:
            return None

    def _poll_fn(self) -> Any:
        # Module global resolved at call time — tests patch
        # ``mcp_combiner.sharedserver._poll_url``.
        return _poll_url

    async def _launch_and_poll(
        self,
        server_name: str,
        entry: "SharedServerConfig",
        url: str | None,
    ) -> None:
        ss = entry  # sharedserver's entries always read better as ss
        try:
            binary = self._get_binary()
        except FileNotFoundError as exc:
            logger.warning("Skipping sharedserver start for '%s': %s", server_name, exc)
            self._backing_state[ss.name] = "spawn_failed"
            return

        cmd = _build_use_cmd(binary, ss, pid=os.getpid())
        logger.warning(
            "Starting sharedserver '%s' via: %s",
            ss.name,
            " ".join(cmd),
        )
        self._backing_state[ss.name] = "spawning"

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode != 0:
                logger.warning(
                    "sharedserver use '%s' exited %d: %s",
                    ss.name,
                    proc.returncode,
                    stderr.decode().strip() if stderr else "",
                )
                self._backing_state[ss.name] = "spawn_failed"
                return
        except asyncio.TimeoutError:
            logger.warning("sharedserver use '%s' timed out", ss.name)
            self._backing_state[ss.name] = "spawn_failed"
            return
        except OSError as exc:
            logger.warning("sharedserver use '%s' failed: %s", ss.name, exc)
            self._backing_state[ss.name] = "spawn_failed"
            return

        self._active.append(ss.name)
        logger.info("sharedserver '%s' started (refcount incremented)", ss.name)

        # Poll for health (the shared frame — state transitions included).
        await self._poll(server_name, ss, url)

    async def _stop(self, entry: "SharedServerConfig", timeout: float) -> None:
        """Drop our reference: ``sharedserver unuse`` (decref).

        ``timeout`` is informational here — the bound lives in ``_stop_one``
        (the whole point of it being short is the shutdown race,
        :attr:`drain_timeout`). A missing binary raises, which
        ``BackingManagerBase.ensure_stopped`` treats as "no transition" so a
        later unuse can still be attempted.
        """
        binary = self._get_binary()
        await self._stop_one(binary, entry.name)

    async def _stop_hard(self, entry: "SharedServerConfig") -> None:
        """Destroy the backing process — ``admin stop --force``."""
        await self._stop_force(entry)

    async def _stop_force(self, ss: "SharedServerConfig") -> None:
        """``sharedserver admin stop --force`` — graceful, then SIGKILL fallback.

        A non-zero exit (e.g. "server is not running") is logged and ignored:
        the goal is simply to ensure the old process is gone before respawning.
        """
        binary = self._get_binary()
        cmd = [binary, "admin", "stop", "--force", ss.name]
        logger.warning("sharedserver admin stop --force '%s'", ss.name)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode != 0:
                logger.info(
                    "sharedserver admin stop '%s' exited %s (continuing): %s",
                    ss.name,
                    proc.returncode,
                    stderr.decode().strip() if stderr else "",
                )
        except (asyncio.TimeoutError, OSError) as exc:
            logger.warning("sharedserver admin stop '%s' failed: %s", ss.name, exc)

    def _shutdown_coros(self) -> list[Any]:
        """One ``unuse`` coro per started entry; a missing binary raises so
        ``stop_all`` keeps the references (as it always did)."""
        binary = self._get_binary()
        return [self._stop_one(binary, name) for name in list(self._active)]

    async def _stop_one(self, binary: str, name: str) -> None:
        cmd = [binary, "unuse", name]
        logger.info("sharedserver unuse '%s'", name)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            # Short: `unuse` is just a refcount decrement and should return fast.
            # Bounded so a concurrent stop_all() completes well inside the ~5s
            # SIGTERM→SIGKILL window sharedserver gives us on shutdown.
            await asyncio.wait_for(proc.communicate(), timeout=2)
        except (asyncio.TimeoutError, OSError) as exc:
            logger.warning("sharedserver unuse '%s' failed: %s", name, exc)


# Module-level manager reference for cleanup
_manager: SharedServerManager | BackingProcessManager | None = None


def register_for_cleanup(manager: SharedServerManager | BackingProcessManager) -> None:
    """Register the backing-process manager for cleanup on process exit.

    Either the plain SharedServerManager or the BackingProcessManager facade
    (sharedserver + external + plain-url probe states). ``cleanup()`` only
    calls ``stop_all()`` on it, duck-typed.
    """
    global _manager
    _manager = manager


def cleanup() -> None:
    """Cleanup sharedserver references on exit.

    Safe to call from signal handlers or atexit. Runs stop_all() to
    decrement reference counts for all started servers.
    """
    global _manager
    if _manager is None:
        return

    try:
        # Run stop_all in a new event loop if needed
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # Schedule cleanup in the running loop
            asyncio.ensure_future(_manager.stop_all())
        else:
            # Create new loop for cleanup
            asyncio.run(_manager.stop_all())
    except Exception as e:
        logger.warning("Failed to cleanup sharedservers: %s", e)
    finally:
        _manager = None
