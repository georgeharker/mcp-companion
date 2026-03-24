"""sharedserver integration for mcp-bridge.

Provides helpers to start and stop HTTP server processes via the ``sharedserver``
CLI tool, and to poll a URL until the server is reachable.

sharedserver owns the process lifetime — the bridge only increments/decrements
the reference count.  Multiple clients (Neovim instances, bridge processes) can
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
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp_bridge.config import BridgeConfig, SharedServerConfig

logger = logging.getLogger("mcp-bridge.sharedserver")


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
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2)
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
    reference (e.g. the bridge process).  sharedserver tracks this PID and
    decrements the refcount when it exits.  Without an explicit ``--pid`` the
    tool defaults to the *caller's* PID, which is the short-lived subprocess
    spawned by :func:`subprocess.run` — that subprocess exits immediately,
    causing the refcount to drop to zero and the server to stop.
    """
    from mcp_bridge.config import _interpolate_dict, _interpolate_list, _interpolate_str

    cmd = [binary, "use", ss.name]

    if ss.grace_period:
        cmd += ["--grace-period", ss.grace_period]

    # Tie the sharedserver reference to the bridge process, not to the
    # short-lived subprocess.run() helper process.
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


class SharedServerManager:
    """Manages sharedserver lifecycle for all servers in a ``BridgeConfig``.

    Usage::

        mgr = SharedServerManager(config)
        await mgr.start_all()
        # ... bridge runs ...
        await mgr.stop_all()
    """

    def __init__(self, config: "BridgeConfig") -> None:
        self._config = config
        self._binary: str | None = None
        # Track which server names we successfully `use`d so we can `unuse` them.
        self._active: list[str] = []

    def _get_binary(self) -> str:
        if self._binary is None:
            self._binary = _require_binary()
        return self._binary

    async def start_all(self) -> None:
        """Call ``sharedserver use`` for every enabled server that has a sharedserver config.

        After starting, polls the server URL until healthy (or timeout).
        Servers that fail to start or become healthy are logged as warnings —
        the bridge continues mounting other servers.
        """
        servers_with_ss = [
            name
            for name in self._config.get_enabled_servers()
            if self._config.resolve_shared_server(name) is not None
        ]
        if servers_with_ss:
            logger.warning(
                "Starting sharedserver-managed servers: %s",
                ", ".join(servers_with_ss),
            )
        else:
            logger.warning("No sharedserver-managed servers configured")

        for name, srv in self._config.get_enabled_servers().items():
            ss = self._config.resolve_shared_server(name)
            if ss is None:
                continue
            await self._start_one(name, ss, srv.url)

    async def _start_one(
        self,
        server_name: str,
        ss: "SharedServerConfig",
        url: str | None,
    ) -> None:
        try:
            binary = self._get_binary()
        except FileNotFoundError as exc:
            logger.warning("Skipping sharedserver start for '%s': %s", server_name, exc)
            return

        cmd = _build_use_cmd(binary, ss, pid=os.getpid())
        logger.warning(
            "Starting sharedserver '%s' via: %s",
            ss.name,
            " ".join(cmd),
        )

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                logger.warning(
                    "sharedserver use '%s' exited %d: %s",
                    ss.name,
                    result.returncode,
                    result.stderr.strip(),
                )
                return
        except subprocess.TimeoutExpired:
            logger.warning("sharedserver use '%s' timed out", ss.name)
            return
        except OSError as exc:
            logger.warning("sharedserver use '%s' failed: %s", ss.name, exc)
            return

        self._active.append(ss.name)
        logger.info("sharedserver '%s' started (refcount incremented)", ss.name)

        # Poll for health
        if url:
            health_url = url.rstrip("/")
            logger.info(
                "Waiting up to %ds for '%s' at %s",
                ss.health_timeout,
                server_name,
                health_url,
            )
            ready = await _poll_url(health_url, ss.health_timeout)
            if ready:
                logger.info("'%s' is healthy", server_name)
            else:
                logger.warning(
                    "'%s' did not become healthy within %ds — "
                    "proxy will be mounted but may fail until server is ready",
                    server_name,
                    ss.health_timeout,
                )

    async def stop_all(self) -> None:
        """Call ``sharedserver unuse`` for every server we started."""
        if not self._active:
            return
        try:
            binary = self._get_binary()
        except FileNotFoundError:
            return

        for name in list(self._active):
            await self._stop_one(binary, name)
        self._active.clear()

    async def _stop_one(self, binary: str, name: str) -> None:
        cmd = [binary, "unuse", name]
        logger.info("sharedserver unuse '%s'", name)
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("sharedserver unuse '%s' failed: %s", name, exc)


# Module-level manager reference for cleanup
_manager: SharedServerManager | None = None


def register_for_cleanup(manager: SharedServerManager) -> None:
    """Register a SharedServerManager for cleanup on process exit."""
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
