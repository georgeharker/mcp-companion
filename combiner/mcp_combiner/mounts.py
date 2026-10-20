"""Explicit bookkeeping of the providers each server mount adds.

fastmcp's ``mount()`` returns nothing and the wrapper it appends to
``combiner.providers`` has no stable public marker carrying the namespace —
its repr/attributes have already drifted across fastmcp versions (the old
repr-sniffing matcher silently matched nothing on fastmcp 3.4, leaving stale
providers mounted after every restart; see COMBINER-RESTART-BUG.md). So we
record exactly which provider objects each mount added, and unmount removes
those same objects by identity.

The registry lives on the combiner instance itself so every holder of the
combiner (startup lifespan, meta-tools) shares one source of truth.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from fastmcp.server.providers.base import Provider

if TYPE_CHECKING:
    from fastmcp import FastMCP

logger = logging.getLogger("mcp-combiner")


class BoundedListProvider(Provider):
    """Wrap one RAW upstream provider so its LISTING calls can't hang.

    The downstream ``tools/list`` path aggregates every mounted provider with
    no timeout of its own (fastmcp ``AggregateProvider``), and a persistent
    upstream that went half-dead (external combiner reset, wedged POST) can
    make ``provider.list_tools()`` await forever. One wedged provider then
    wedges tools/list for EVERY downstream session. The per-call
    ``asyncio.wait_for`` here turns that into a logged, skipped error — a
    wedged server is simply absent from the aggregate listing until its
    health monitor reconnects it, and the log names the culprit.

    The namespace transform (added by ``add_provider(namespace=…)``) is applied
    OUTSIDE this wrapper, so every method below delegates the RAW name to the
    raw provider exactly as fastmcp's own ``_WrappedProvider`` does. Only the
    listing (and ``get``-style metadata) ops are bounded: resolution must not
    hang either, while the tool RUN stays unbounded — legitimate tool calls
    take minutes.

    Mirroring ``_WrappedProvider``, the ``_list_*``/``_get_*`` surface is
    explicitly overridden: the base ``Provider`` implements ALL of them with
    default (None/empty) bodies, so a generic ``__getattr__`` fallback never
    fires for them and call routing would silently break ("Unknown tool").
    """

    def __init__(self, inner: Any, timeout: float | None = None) -> None:
        super().__init__()
        if timeout is None:
            from mcp_combiner.toolcache import UPSTREAM_TOOL_LIST_TIMEOUT

            timeout = UPSTREAM_TOOL_LIST_TIMEOUT
        self._inner = inner
        self._timeout = timeout

    def __getattr__(self, name: str) -> Any:
        # Only reached for names not found locally or on Provider — delegate
        # everything else (wrap_transform, lifespan, ...) to the raw provider.
        return getattr(self.__dict__["_inner"], name)

    def __repr__(self) -> str:
        return f"BoundedListProvider({self._inner!r}, timeout={self._timeout!r})"

    # -- bounded listing / metadata resolution ------------------------------

    def _bounded(self, op: str, *args: Any) -> Any:
        inner_op = getattr(self._inner, op, None)
        if inner_op is None:
            return None

        async def bounded() -> Any:
            try:
                return await asyncio.wait_for(inner_op(*args), timeout=self._timeout)
            except asyncio.TimeoutError as exc:
                logger.warning(
                    "Provider listing '%s' did not answer within %.0fs — "
                    "skipped from the aggregate fetch (upstream wedged or "
                    "still reconnecting; its health monitor will recover it)",
                    op,
                    self._timeout,
                )
                raise TimeoutError(
                    f"{op} from {self._inner!r} did not answer within {self._timeout:.0f}s"
                ) from exc

        return bounded()

    async def list_tools(self) -> Any:
        return await self._bounded("list_tools") or []

    async def _list_tools(self) -> Any:
        return await self._bounded("list_tools") or []

    async def get_tool(self, name: str, version: Any = None) -> Any:
        return await self._bounded("get_tool", name, version)

    async def _get_tool(self, name: str, version: Any = None) -> Any:
        return await self._bounded("get_tool", name, version)

    async def get_tool_by_hash(self, tool_hash: str, tool_name: str) -> Any:
        return await self._bounded("get_tool_by_hash", tool_hash, tool_name)

    async def _get_tool_by_hash(self, tool_hash: str, tool_name: str) -> Any:
        return await self._bounded("get_tool_by_hash", tool_hash, tool_name)

    async def get_app_tool(self, app_name: str, tool_name: str) -> Any:
        return await self._bounded("get_app_tool", app_name, tool_name)

    async def list_resources(self) -> Any:
        return await self._bounded("list_resources") or []

    async def _list_resources(self) -> Any:
        return await self._bounded("list_resources") or []

    async def get_resource(self, uri: str, version: Any = None) -> Any:
        return await self._bounded("get_resource", uri, version)

    async def _get_resource(self, uri: str, version: Any = None) -> Any:
        return await self._bounded("get_resource", uri, version)

    async def list_resource_templates(self) -> Any:
        return await self._bounded("list_resource_templates") or []

    async def _list_resource_templates(self) -> Any:
        return await self._bounded("list_resource_templates") or []

    async def get_resource_template(self, uri: str, version: Any = None) -> Any:
        return await self._bounded("get_resource_template", uri, version)

    async def _get_resource_template(self, uri: str, version: Any = None) -> Any:
        return await self._bounded("get_resource_template", uri, version)

    async def list_prompts(self) -> Any:
        return await self._bounded("list_prompts") or []

    async def _list_prompts(self) -> Any:
        return await self._bounded("list_prompts") or []

    async def get_prompt(self, name: str, version: Any = None) -> Any:
        return await self._bounded("get_prompt", name, version)

    async def _get_prompt(self, name: str, version: Any = None) -> Any:
        return await self._bounded("get_prompt", name, version)

    # -- unbounded, forwarded verbatim --------------------------------------

    async def get_tasks(self) -> Any:
        return await self._inner.get_tasks()


_REGISTRY_ATTR = "_mounted_provider_registry"


def _registry(combiner: Any) -> dict[str, list[object]]:
    reg = getattr(combiner, _REGISTRY_ATTR, None)
    if reg is None:
        reg = {}
        setattr(combiner, _REGISTRY_ATTR, reg)
    return reg


def _sniff_namespace(provider: object, server_name: str) -> bool:
    """Best-effort fallback match for providers mounted outside the registry.

    fastmcp wraps namespaced mounts as ``_WrappedProvider(...,
    transforms=[Namespace(name)])`` where the name lives in the transform's
    private ``_prefix``. Private-attr sniffing has the same drift risk that
    caused the original bug, so this is only a safety net — anything it
    catches means a mount bypassed ``mount_server_provider`` and gets logged.
    """
    for transform in getattr(provider, "transforms", None) or []:
        if getattr(transform, "_prefix", None) == server_name:
            return True
    return False


def mount_server_provider(combiner: FastMCP, proxy: Any, server_name: str) -> None:
    """Mount *proxy* under *server_name* and record the providers it added.

    Equivalent to ``combiner.mount(proxy, namespace=server_name)`` — the mount's
    FastMCPProvider + Namespace wrapping — except the FastMCPProvider is
    wrapped in ``BoundedListProvider`` FIRST, so the namespace transform sits
    OUTSIDE the bound and every lookup stays fastmcp-shaped on raw names (a
    ``Bounded`` around the already-wrapped provider breaks transform
    composition: base-class ``transforms`` reads the wrapper's own state and
    get_tool routing goes dead).

    Any providers already registered for the namespace are dropped first, so
    a re-mount (enable while mounted, restart) can never leave two providers
    resolving the same tool names.
    """
    stale = drop_server_providers(combiner, server_name)
    if stale:
        logger.warning(
            "Server '%s': dropped %d stale provider(s) before remount",
            server_name,
            stale,
        )
    from fastmcp import FastMCP
    from fastmcp.server.providers.fastmcp_provider import FastMCPProvider

    raw = FastMCPProvider(proxy) if isinstance(proxy, FastMCP) else proxy
    before = list(combiner.providers)
    combiner.add_provider(BoundedListProvider(raw), namespace=server_name)
    added = [p for p in combiner.providers if not any(p is q for q in before)]
    if not added:
        logger.warning(
            "Server '%s': add_provider() added no providers — unmount "
            "bookkeeping will have nothing to remove",
            server_name,
        )
        return
    # The registry records exactly the objects add_provider appended (the
    # namespaced wrappers around our bounded provider); dropping stays
    # identity-based.
    _registry(combiner).setdefault(server_name, []).extend(added)


def get_server_providers(combiner: FastMCP, server_name: str) -> list[Any]:
    """The providers currently mounted for *server_name* (read-only view).

    A provider's ``list_tools()`` yields tools exactly as an aggregate fetch
    sees them — namespaced by fastmcp's own transform — which makes it the
    right handle for priming/storing a server's tool slice without
    reimplementing the namespacing.
    """
    return list(_registry(combiner).get(server_name, []))


def drop_server_providers(combiner: FastMCP, server_name: str) -> int:
    """Remove all providers mounted for *server_name*, by object identity.

    Returns the number actually removed from ``combiner.providers``.
    """
    recorded = _registry(combiner).pop(server_name, [])
    unrecorded = [
        p
        for p in combiner.providers
        if _sniff_namespace(p, server_name) and not any(p is r for r in recorded)
    ]
    if unrecorded:
        logger.warning(
            "Server '%s': found %d mounted provider(s) missing from the mount "
            "registry — some mount path bypassed mount_server_provider()",
            server_name,
            len(unrecorded),
        )
    stale = recorded + unrecorded
    if not stale:
        return 0
    before = len(combiner.providers)
    combiner.providers = [p for p in combiner.providers if not any(p is s for s in stale)]
    removed = before - len(combiner.providers)
    if removed < len(recorded):
        logger.warning(
            "Server '%s': mount registry recorded %d provider(s) but only %d "
            "were present in combiner.providers",
            server_name,
            len(recorded),
            removed,
        )
    return removed
