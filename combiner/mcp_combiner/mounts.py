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

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastmcp import FastMCP

logger = logging.getLogger("mcp-combiner")

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
    before = list(combiner.providers)
    combiner.mount(proxy, namespace=server_name)
    added = [p for p in combiner.providers if not any(p is q for q in before)]
    if not added:
        logger.warning(
            "Server '%s': mount() added no providers — unmount bookkeeping "
            "will have nothing to remove",
            server_name,
        )
    _registry(combiner).setdefault(server_name, []).extend(added)


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
