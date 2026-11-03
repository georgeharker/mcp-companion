"""Hear a 2026-era upstream between calls: ``subscriptions/listen`` upstream.

On the handshake revisions an upstream pushes ``tools/list_changed``,
``resources/updated`` and friends over its session's standing stream. The
2026-07-28 revision has no session and no standing stream: a server can only
answer requests, and a client that wants to hear about changes between calls
opens ``subscriptions/listen`` itself, naming what it wants (list changes, and
the resource URIs it watches). The server sends only what was asked for.

So the combiner, as the client, keeps one listen stream open to each 2026-era
upstream (see ``connections._open``):

- list changes (tools, prompts, resources) — always: the combiner needs them to
  keep its own catalogue current whether or not any chat is listening;
- resource updates — for the URIs some chat watches (:data:`INTEREST`), which
  comes from 2026-era chats' own listen streams (``listen.py``) and from
  handshake-era chats' ``resources/subscribe`` (:func:`register_subscribe`).
  The filter is fixed per stream, so a change in what's watched reopens it.

What arrives is forwarded exactly as a handshake upstream's notifications are
(``notifications._ResourceNotifyHandler``): tool changes re-prime the server's
cached slice, the rest goes to every chat (a 2026 upstream is one shared
connection). A dropped stream is reopened with backoff and the tool list is
re-primed, since changes during the gap were never sent.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import weakref
from collections.abc import Callable, Iterable
from typing import Any

import mcp.types
from fastmcp import Client, FastMCP
from mcp.client.subscriptions import listen
from mcp.server.connection import Connection
from mcp.server.context import ServerRequestContext
from mcp.shared.exceptions import MCPError
from mcp.shared.subscriptions import (
    PromptsListChanged,
    ResourcesListChanged,
    ResourceUpdated,
    ServerEvent,
    ToolsListChanged,
)

from mcp_combiner.runtime import downstream_connection
from mcp_combiner.toolcache import un_namespace_uri

logger = logging.getLogger("mcp-combiner")

_INITIAL_BACKOFF_S = 1.0
_MAX_BACKOFF_S = 30.0


class ResourceInterest:
    """Which (namespaced) resource URIs some chat currently watches, counted."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._watchers: list[Callable[[], None]] = []

    def add(self, uris: Iterable[str]) -> None:
        new = False
        for uri in uris:
            count = self._counts.get(uri, 0)
            self._counts[uri] = count + 1
            new = new or count == 0
        if new:
            self._changed()

    def remove(self, uris: Iterable[str]) -> None:
        gone = False
        for uri in uris:
            count = self._counts.get(uri, 0)
            if count <= 1:
                gone = gone or self._counts.pop(uri, None) is not None
            else:
                self._counts[uri] = count - 1
        if gone:
            self._changed()

    def watched(self) -> frozenset[str]:
        return frozenset(self._counts)

    def upstream_uris(self, server: str) -> list[str]:
        """The watched URIs that belong to *server*, as the server names them."""
        found = (un_namespace_uri(uri, server) for uri in self._counts)
        return sorted(uri for uri in found if uri is not None)

    def on_change(self, watcher: Callable[[], None]) -> Callable[[], None]:
        self._watchers.append(watcher)

        def undo() -> None:
            with contextlib.suppress(ValueError):
                self._watchers.remove(watcher)

        return undo

    def reset(self) -> None:
        self._counts.clear()
        self._watchers.clear()

    def _changed(self) -> None:
        for watcher in list(self._watchers):
            watcher()


INTEREST = ResourceInterest()


class UpstreamListener:
    """The standing ``subscriptions/listen`` stream to one 2026-era upstream."""

    def __init__(self, name: str, client: Client[Any]) -> None:
        self._name = name
        self._client = client
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"listen-upstream-{self._name}")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _run(self) -> None:
        backoff = _INITIAL_BACKOFF_S
        reopening = False
        while True:
            changed = asyncio.Event()
            undo = INTEREST.on_change(changed.set)
            stream = asyncio.create_task(self._listen_once(catch_up=reopening))
            watch = asyncio.create_task(changed.wait())
            try:
                await asyncio.wait({stream, watch}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                undo()
                watch.cancel()
                if not stream.done():
                    # What's watched changed (or we're stopping): end this stream.
                    stream.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await stream
            reopening = True
            if stream.cancelled():
                backoff = _INITIAL_BACKOFF_S
                continue  # reopen at once with the new URI set
            error = stream.exception()
            if isinstance(error, MCPError) and error.error.code == mcp.types.METHOD_NOT_FOUND:
                logger.info(
                    "Upstream '%s' doesn't serve subscriptions/listen: no change "
                    "notifications between calls",
                    self._name,
                )
                return
            logger.info(
                "Upstream '%s': listen stream ended (%s); reopening in %.0fs",
                self._name,
                "closed by the server" if error is None else f"{type(error).__name__}: {error}",
                backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF_S)

    async def _listen_once(self, *, catch_up: bool) -> None:
        """One listen stream, until it ends."""
        uris = INTEREST.upstream_uris(self._name)
        async with listen(
            self._client.session,
            tools_list_changed=True,
            prompts_list_changed=True,
            resources_list_changed=True,
            resource_subscriptions=uris,
        ) as subscription:
            logger.info("Upstream '%s': listening (%d watched resource(s))", self._name, len(uris))
            if catch_up:
                # Changes made while no stream was open were never sent.
                await self._dispatch(ToolsListChanged())
            async for event in subscription:
                await self._dispatch(event)

    async def _dispatch(self, event: ServerEvent) -> None:
        from mcp_combiner.notifications import _ResourceNotifyHandler

        handler = _ResourceNotifyHandler(self._name)  # shared upstream: every chat
        try:
            if isinstance(event, ToolsListChanged):
                await handler.on_tool_list_changed(mcp.types.ToolListChangedNotification())
            elif isinstance(event, PromptsListChanged):
                await handler.on_prompt_list_changed(mcp.types.PromptListChangedNotification())
            elif isinstance(event, ResourcesListChanged):
                await handler.on_resource_list_changed(mcp.types.ResourceListChangedNotification())
            elif isinstance(event, ResourceUpdated):
                await handler.on_resource_updated(
                    mcp.types.ResourceUpdatedNotification(
                        params=mcp.types.ResourceUpdatedNotificationParams(uri=event.uri)
                    )
                )
        except Exception:
            # Forwarding must never end the stream.
            logger.debug("Forwarding %r from '%s' failed", event, self._name, exc_info=True)


# ── handshake-era chats: resources/subscribe ───────────────────────

_SUBSCRIBED: weakref.WeakKeyDictionary[Connection, set[str]] = weakref.WeakKeyDictionary()


def _release(uris: set[str]) -> None:
    INTEREST.remove(list(uris))


async def _subscribe(
    ctx: ServerRequestContext[Any, Any], params: mcp.types.SubscribeRequestParams
) -> mcp.types.EmptyResult:
    connection = downstream_connection(ctx.session)
    if connection is not None:
        uris = _SUBSCRIBED.get(connection)
        if uris is None:
            uris = _SUBSCRIBED[connection] = set()
            # A chat that goes away without unsubscribing stops watching.
            weakref.finalize(connection, _release, uris)
        uri = str(params.uri)
        if uri not in uris:
            uris.add(uri)
            INTEREST.add([uri])
    return mcp.types.EmptyResult()


async def _unsubscribe(
    ctx: ServerRequestContext[Any, Any], params: mcp.types.UnsubscribeRequestParams
) -> mcp.types.EmptyResult:
    connection = downstream_connection(ctx.session)
    uris = _SUBSCRIBED.get(connection) if connection is not None else None
    uri = str(params.uri)
    if uris is not None and uri in uris:
        uris.discard(uri)
        INTEREST.remove([uri])
    return mcp.types.EmptyResult()


def register_subscribe(combiner: FastMCP[Any]) -> None:
    """Serve handshake-era ``resources/subscribe`` / ``unsubscribe`` (which also
    advertises ``resources.subscribe``). Handshake upstreams keep pushing
    unasked; what this records is for 2026-era upstreams, which only send
    updates for URIs someone listens for."""
    server = combiner._mcp_server
    server.add_request_handler("resources/subscribe", mcp.types.SubscribeRequestParams, _subscribe)
    server.add_request_handler(
        "resources/unsubscribe", mcp.types.UnsubscribeRequestParams, _unsubscribe
    )
