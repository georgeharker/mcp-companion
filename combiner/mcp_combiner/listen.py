"""``subscriptions/listen`` for 2026-07-28 clients (the era translation layer).

On the 2026 revision a client receives change notifications only on streams it
opens with ``subscriptions/listen``; there is no long-lived connection to push
to. Every place the combiner pushes to handshake-era connections
(``toolcache``, ``notifications``, ``meta_tools``) also publishes to the
:data:`HUB` here, which fans events out to the open listen streams:

* **broadcast** events (the combined tool list changed, a shared upstream's
  resource changed) reach every stream;
* **per-chat** events (a chat's filter changed, its isolated upstream's
  resource changed) reach only the streams opened under that chat's token.

A listen stream belongs to the chat token its request carries (``/mcp/<token>``
or ``X-MCP-Combiner-Session``) — the same identity everything chat-scoped is
keyed by. Streams without a token receive broadcasts only. Serving uses the
SDK's own ``ListenHandler`` (ack first, subscription-id stamping, per-stream
filtering, bounded backlog), one per chat over a token-scoped view of the hub.
A client that never listens polls instead, which the tool cache keeps cheap.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from mcp.server.context import ServerRequestContext
from mcp.server.subscriptions import (
    InMemorySubscriptionBus,
    ListenHandler,
    PromptsListChanged,
    ResourcesListChanged,
    ResourceUpdated,
    ServerEvent,
    ToolsListChanged,
)
from mcp.types import SubscriptionsListenRequestParams, SubscriptionsListenResult

from mcp_combiner.isolated import TOKEN_HEADER

logger = logging.getLogger("mcp-combiner")

__all__ = [
    "HUB",
    "ChatEventHub",
    "PromptsListChanged",
    "ResourceUpdated",
    "ResourcesListChanged",
    "ToolsListChanged",
    "register_listen",
]


class _ChatView:
    """A ``SubscriptionBus`` over the hub as one chat sees it: broadcasts plus
    the chat's own events (only broadcasts when *token* is None)."""

    def __init__(self, hub: ChatEventHub, token: str | None) -> None:
        self._hub = hub
        self._token = token

    async def publish(self, event: ServerEvent) -> None:
        await self._hub.publish(event, token=self._token)

    def subscribe(self, listener: Callable[[ServerEvent], None]) -> Callable[[], None]:
        unsubscribes = [self._hub.broadcast_bus.subscribe(listener)]
        if self._token is not None:
            unsubscribes.append(self._hub.chat_bus(self._token).subscribe(listener))

        def unsubscribe() -> None:
            for undo in unsubscribes:
                undo()

        return unsubscribe


class ChatEventHub:
    """Broadcast + per-chat fan-out to 2026-era listen streams."""

    def __init__(self) -> None:
        self.broadcast_bus = InMemorySubscriptionBus()
        self._chat_buses: dict[str, InMemorySubscriptionBus] = {}
        self._handlers: dict[str | None, ListenHandler] = {}

    def chat_bus(self, token: str) -> InMemorySubscriptionBus:
        bus = self._chat_buses.get(token)
        if bus is None:
            bus = self._chat_buses[token] = InMemorySubscriptionBus()
        return bus

    async def publish(self, event: ServerEvent, *, token: str | None = None) -> None:
        """Deliver *event* to every listen stream (``token=None``) or only to
        streams opened under *token*. Never raises into the publisher."""
        bus = self.broadcast_bus if token is None else self._chat_buses.get(token)
        if bus is None:
            return  # no stream ever listened for that chat
        try:
            await bus.publish(event)
        except Exception:
            logger.debug("listen: publishing %r failed", event, exc_info=True)

    def handler_for(self, token: str | None) -> ListenHandler:
        handler = self._handlers.get(token)
        if handler is None:
            handler = self._handlers[token] = ListenHandler(_ChatView(self, token))
        return handler

    def close(self) -> None:
        """End every open listen stream gracefully (combiner shutdown)."""
        for handler in self._handlers.values():
            handler.close()


#: The combiner's one hub.
HUB = ChatEventHub()


def _listen_token() -> str | None:
    try:
        return get_http_headers().get(TOKEN_HEADER) or None
    except Exception:
        return None


async def _serve_listen(
    ctx: ServerRequestContext[Any, Any], params: SubscriptionsListenRequestParams
) -> SubscriptionsListenResult:
    # The URIs this stream watches: 2026-era upstreams only send updates for
    # URIs the combiner listens for in turn (see upstream_listen).
    from mcp_combiner.upstream_listen import INTEREST

    uris = list(params.notifications.resource_subscriptions or ())
    INTEREST.add(uris)
    try:
        return await HUB.handler_for(_listen_token())(ctx, params)
    finally:
        INTEREST.remove(uris)


def register_listen(combiner: FastMCP[Any]) -> None:
    """Serve ``subscriptions/listen`` from :data:`HUB` (also makes the SDK
    advertise ``listChanged`` / ``subscribe`` to 2026-era clients)."""
    combiner._mcp_server.add_request_handler(
        "subscriptions/listen", SubscriptionsListenRequestParams, _serve_listen
    )
