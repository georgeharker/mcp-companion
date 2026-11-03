"""Paginated list responses (``tools/list``, ``resources/list``, …).

Some upstreams list thousands of resources (a metrics server exposing one per
metric). Unpaginated, the combiner's aggregate ``resources/list`` became one
JSON-RPC message — one SSE event — of tens of megabytes, past clients' event
limits (pi's is 16 MiB). So list responses are paginated: at most
:func:`list_page_size` items per page (``MCP_COMBINER_LIST_PAGE_SIZE``,
default 1000; ``0`` turns paging off), with a ``nextCursor`` the client follows.

fastmcp paginates by offset, re-running the full listing for every page. For
tools that is a cache hit; resources are fanned out to every upstream, so the
first page's full listing is kept per chat (:class:`ResourceListSnapshots`)
and the later pages of that walk are cut from it — consistent pages, one
upstream fan-out per walk.
"""

from __future__ import annotations

import os
import time
from collections.abc import Sequence
from typing import Any

import mcp.types as mt
from fastmcp.resources import Resource
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

PAGE_SIZE_ENV = "MCP_COMBINER_LIST_PAGE_SIZE"
DEFAULT_PAGE_SIZE = 1000
#: How long a walk's snapshot serves later pages (a client fetches them back to back).
SNAPSHOT_TTL_S = 120.0
_MAX_SNAPSHOTS = 256


def list_page_size() -> int | None:
    """Items per list page, or ``None`` for no paging."""
    raw = os.environ.get(PAGE_SIZE_ENV, "").strip()
    if not raw:
        return DEFAULT_PAGE_SIZE
    try:
        size = int(raw)
    except ValueError:
        return DEFAULT_PAGE_SIZE
    return size if size > 0 else None


class ResourceListSnapshots:
    """The full resource listing of each chat's current list walk."""

    def __init__(self) -> None:
        self._walks: dict[str, tuple[float, Sequence[Resource]]] = {}

    def keep(self, key: str, resources: Sequence[Resource]) -> None:
        now = time.monotonic()
        self._walks = {k: v for k, v in self._walks.items() if now - v[0] < SNAPSHOT_TTL_S}
        if len(self._walks) >= _MAX_SNAPSHOTS:
            del self._walks[min(self._walks, key=lambda k: self._walks[k][0])]
        self._walks[key] = (now, resources)

    def get(self, key: str) -> Sequence[Resource] | None:
        entry = self._walks.get(key)
        if entry is None or time.monotonic() - entry[0] >= SNAPSHOT_TTL_S:
            return None
        return entry[1]

    def reset(self) -> None:
        self._walks.clear()


SNAPSHOTS = ResourceListSnapshots()


class ResourceListPagingMiddleware(Middleware):
    """Serve the later pages of a ``resources/list`` walk from its first page's listing."""

    async def on_list_resources(
        self,
        context: MiddlewareContext[mt.ListResourcesRequest],
        call_next: CallNext[mt.ListResourcesRequest, Sequence[Resource]],
    ) -> Sequence[Resource]:
        page_size = list_page_size()
        key = _walk_key(context)
        if page_size is None or key is None:
            return await call_next(context)
        if _cursor(context) is not None:
            snapshot = SNAPSHOTS.get(key)
            if snapshot is not None:
                return snapshot
        resources = await call_next(context)
        if len(resources) > page_size:
            SNAPSHOTS.keep(key, resources)
        return resources


def _walk_key(context: MiddlewareContext[Any]) -> str | None:
    """The chat whose walk this is: its token, else its connection."""
    from mcp_combiner.middleware import _chat_token
    from mcp_combiner.runtime import downstream_connection

    token = _chat_token()
    if token:
        return f"token:{token}"
    fctx = context.fastmcp_context
    rc = fctx.request_context if fctx is not None else None
    connection = downstream_connection(rc.session) if rc is not None else None
    return f"conn:{id(connection)}" if connection is not None else None


def _cursor(context: MiddlewareContext[Any]) -> str | None:
    """The request's pagination cursor (fastmcp's list middleware sees no params)."""
    fctx = context.fastmcp_context
    rc = fctx.request_context if fctx is not None else None
    params = rc._srctx.params if rc is not None else None
    cursor = params.get("cursor") if isinstance(params, dict) else None
    return cursor if isinstance(cursor, str) and cursor else None
