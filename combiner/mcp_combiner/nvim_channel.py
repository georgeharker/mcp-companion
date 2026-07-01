"""Back-channel from the combiner into live Neovim instances.

The combiner is a single shared process serving N Neovim instances. To let an
external agent *control* an editor, the combiner must reach back into the specific
instance that owns a chat. This module owns that link.

Design (see docs/designs/native-neovim-server.md):

* One pynvim connection per ``instance_id`` (the instance's private msgpack-RPC
  socket), created lazily and evicted on connection error.
* A **per-instance FIFO queue + single worker** serialises calls to each
  instance — at most one in-flight RPC per instance, executed in order. This is
  the combiner-side half of the ordering guarantee; Neovim's main loop is the
  other half (see the re-entrancy/loop-back notes in the design doc).
* The combiner's *only* RPC into Neovim is the single ``dispatch`` entry point —
  never raw ``vim.api``. Tool curation + (absence of) approval live in Lua.

Phase 2 (fast-sync) uses ``asyncio.to_thread`` around synchronous pynvim, which
the design explicitly permits while we ship only fast, pre-approved tools. The
async msgpack session + ``rpcnotify`` deferred path is a later hardening for
long-running jobs; it is intentionally not here yet.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pynvim

logger = logging.getLogger("mcp-combiner.nvim")

# The single curation boundary. Args arrive as `...` from exec_lua.
_DISPATCH_LUA = "return require('mcp_companion.native').dispatch(...)"

# Tool/resource manifest fetch — a trivial read over the same channel, run once
# per instance at first need and cached on the instance.
_MANIFEST_LUA = "return require('mcp_companion.native').manifest()"

# Default per-call deadline. Fast tools should resolve well within this; a call
# that exceeds it fails and the queue advances (no busy-wait, no parked thread).
DEFAULT_TIMEOUT = 5.0


class NoInstanceError(RuntimeError):
    """Raised when a call targets an instance that is not registered."""


class _Instance:
    """A registered Neovim instance: its socket, lazy connection, and queue."""

    def __init__(self, instance_id: str, socket: str, meta: dict[str, Any] | None = None) -> None:
        self.instance_id = instance_id
        self.socket = socket
        self.meta = meta or {}  # human-meaningful metadata: cwd, name, pid, …
        self.nvim: pynvim.Nvim | None = None
        self.queue: asyncio.Queue[_Job] = asyncio.Queue()
        self.worker: asyncio.Task[None] | None = None


class _Job:
    """A queued Lua call awaiting its turn on an instance's worker.

    Every interaction with an instance — tool dispatch and manifest fetch alike —
    runs as a job through the per-instance FIFO queue, so they are strictly
    ordered and never overlap on the (non-threadsafe) pynvim connection.
    """

    __slots__ = ("label", "lua", "lua_args", "timeout", "future")

    def __init__(
        self,
        label: str,
        lua: str,
        lua_args: list[Any],
        timeout: float,
        future: asyncio.Future[Any],
    ) -> None:
        self.label = label
        self.lua = lua
        self.lua_args = lua_args
        self.timeout = timeout
        self.future = future


class NvimChannelManager:
    """Registry of Neovim instances + per-instance serialised dispatch."""

    def __init__(self, default_timeout: float = DEFAULT_TIMEOUT) -> None:
        self._instances: dict[str, _Instance] = {}
        self._default_timeout = default_timeout
        # The tool/resource catalog is captured once from whichever instance
        # connects first, then frozen for the life of the combiner process. The
        # lock makes the first-fetch single-flight under concurrent tools/list.
        self._manifest: dict[str, Any] | None = None
        self._manifest_lock = asyncio.Lock()

    # -- registration -------------------------------------------------------

    def register(self, instance_id: str, socket: str, meta: dict[str, Any] | None = None) -> None:
        """Register (or update) an instance and start its queue worker."""
        existing = self._instances.get(instance_id)
        if existing is not None:
            if meta:
                existing.meta = meta
            if existing.socket != socket:
                logger.info("nvim instance %s socket changed; reconnecting", instance_id)
                existing.socket = socket
                self._close(existing)
            return

        inst = _Instance(instance_id, socket, meta)
        inst.worker = asyncio.create_task(self._run(inst))
        self._instances[instance_id] = inst
        logger.info("registered nvim instance %s at %s", instance_id, socket)

    def deregister(self, instance_id: str) -> None:
        """Remove an instance, cancel its worker, and fail any queued calls."""
        inst = self._instances.pop(instance_id, None)
        if inst is None:
            return
        if inst.worker is not None:
            inst.worker.cancel()
        self._close(inst)
        # Fail anything still queued so callers don't hang.
        while not inst.queue.empty():
            job = inst.queue.get_nowait()
            if not job.future.done():
                job.future.set_exception(NoInstanceError(f"instance {instance_id} deregistered"))
        logger.info("deregistered nvim instance %s", instance_id)

    def has_instance(self, instance_id: str) -> bool:
        return instance_id in self._instances

    def instance_ids(self) -> list[str]:
        return list(self._instances)

    def instances(self) -> list[dict[str, Any]]:
        """List registered instances with their metadata, for agent selection."""
        return [{"instance_id": inst.instance_id, **inst.meta} for inst in self._instances.values()]

    # -- calling ------------------------------------------------------------

    async def call(
        self,
        instance_id: str,
        tool: str,
        args: dict[str, Any] | None = None,
        ctx: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Enqueue a tool call for an instance and await its result.

        Returns the MCP-shaped result table produced by the Lua dispatcher
        (a dict with a ``content`` list, possibly ``isError``). Raises
        ``NoInstanceError`` if the instance is gone, ``TimeoutError`` if the
        call exceeds its deadline, or the underlying transport error.
        """
        return await self._enqueue(
            instance_id,
            label=tool,
            lua=_DISPATCH_LUA,
            lua_args=[tool, args or {}, ctx or {}],
            timeout=timeout,
        )

    async def ensure_manifest(self) -> dict[str, Any] | None:
        """Return the frozen tool/resource catalog, capturing it once.

        On first call the manifest is fetched from whichever instance is
        registered and then **locked for the life of the combiner process** —
        subsequent instances never change it. Returns None only if no instance
        has ever been available to capture from yet (so the combiner advertises no
        ``neovim`` tools until the first editor connects).
        """
        if self._manifest is not None:
            return self._manifest
        async with self._manifest_lock:
            if self._manifest is not None:
                return self._manifest
            for instance_id in list(self._instances):
                try:
                    self._manifest = await self._enqueue(
                        instance_id, label="__manifest__", lua=_MANIFEST_LUA, lua_args=[]
                    )
                    logger.info("captured frozen nvim manifest from instance %s", instance_id)
                    return self._manifest
                except Exception as exc:  # noqa: BLE001 — try the next instance
                    logger.warning("manifest fetch from %s failed: %s", instance_id, exc)
                    continue
            return None

    def manifest(self) -> dict[str, Any] | None:
        """Return the already-captured manifest without fetching (None if unset)."""
        return self._manifest

    # -- internals ----------------------------------------------------------

    async def _enqueue(
        self,
        instance_id: str,
        *,
        label: str,
        lua: str,
        lua_args: list[Any],
        timeout: float | None = None,
    ) -> Any:
        inst = self._instances.get(instance_id)
        if inst is None:
            raise NoInstanceError(f"no Neovim instance registered for {instance_id}")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        await inst.queue.put(_Job(label, lua, lua_args, timeout or self._default_timeout, future))
        return await future

    async def _run(self, inst: _Instance) -> None:
        """Single worker per instance: drain the queue strictly in order."""
        while True:
            job = await inst.queue.get()
            try:
                result = await asyncio.wait_for(self._exec(inst, job), job.timeout)
                if not job.future.done():
                    job.future.set_result(result)
            except asyncio.TimeoutError:
                logger.warning(
                    "nvim %s: %s timed out after %.1fs",
                    inst.instance_id,
                    job.label,
                    job.timeout,
                )
                # Transport may be wedged or the late reply could desync the
                # sync connection — drop it and reconnect fresh next call.
                self._close(inst)
                if not job.future.done():
                    job.future.set_exception(
                        TimeoutError(f"'{job.label}' timed out after {job.timeout}s")
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — surfaced to the caller
                logger.warning("nvim %s: %s failed: %s", inst.instance_id, job.label, exc)
                self._close(inst)  # poison the connection; reconnect next call
                if not job.future.done():
                    job.future.set_exception(exc)
            finally:
                inst.queue.task_done()

    async def _exec(self, inst: _Instance, job: _Job) -> Any:
        """Ensure a connection, then run the job's Lua over the channel."""
        nvim = inst.nvim
        if nvim is None:
            nvim = await asyncio.to_thread(pynvim.attach, "socket", path=inst.socket, decode=True)
            inst.nvim = nvim
        assert nvim is not None  # narrow for the type checker; reconnect guarantees it
        return await asyncio.to_thread(nvim.exec_lua, job.lua, *job.lua_args)

    @staticmethod
    def _close(inst: _Instance) -> None:
        """Drop the connection so the next call reconnects."""
        if inst.nvim is not None:
            try:
                inst.nvim.close()
            except Exception:  # noqa: BLE001 — best-effort
                pass
            inst.nvim = None
