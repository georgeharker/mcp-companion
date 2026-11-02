"""Forward-namespacing of upstream resource notifications (pass 1, forward-only).

Pins the two subtle correctness properties from the svg-mcp addendum:

* **Forward, namespaced** — resources/updated + resources/list_changed reach
  the downstream connection(s) with the URI forward-namespaced the same way the
  mount namespaces ``resources/list``.
* **Survive the clone** — ``Client.new()`` (the per-chat clone) rebuilds the
  handler from the parent's constructor-time one, dropping ours;
  ``attach_resource_forwarding`` re-installs it bound to the clone's own chat,
  and is a no-op when the handler already targets that chat.
"""

from __future__ import annotations

import gc
import inspect
from collections.abc import Iterator
from typing import Any

import pytest
from fakes import RecordingOutbound, recording_connection
from fastmcp import Client
from fastmcp.server.transforms.namespace import Namespace
from mcp.server.subscriptions import ServerEvent
from mcp.types import (
    LoggingMessageNotification,
    LoggingMessageNotificationParams,
    ResourceListChangedNotification,
    ResourceUpdatedNotification,
    ResourceUpdatedNotificationParams,
    ToolListChangedNotification,
)

from mcp_combiner.listen import HUB, ResourcesListChanged, ResourceUpdated
from mcp_combiner.notifications import (
    _ResourceNotifyHandler,
    attach_resource_forwarding,
    forwarding_factory,
)
from mcp_combiner.runtime import CLIENT_CONNECT_MODE, RUNTIME

WIDGET_URI = "ui://svg-mcp/preview"
NAMESPACED = Namespace("svg-mcp")._transform_uri(WIDGET_URI)


class _FakeClient:
    """Stand-in Client: just the ``_session_kwargs`` slot attach writes."""

    def __init__(self) -> None:
        # A real FastMCP client always has the message_handler key present.
        self._session_kwargs: dict[str, Any] = {"message_handler": None}


def _updated(uri: str = WIDGET_URI) -> ResourceUpdatedNotification:
    return ResourceUpdatedNotification(
        method="notifications/resources/updated",
        params=ResourceUpdatedNotificationParams(uri=uri),
    )


def _list_changed() -> ResourceListChangedNotification:
    return ResourceListChangedNotification(method="notifications/resources/list_changed")


def _tools_changed() -> ToolListChangedNotification:
    return ToolListChangedNotification(method="notifications/tools/list_changed")


def _log() -> LoggingMessageNotification:
    return LoggingMessageNotification(
        method="notifications/message",
        params=LoggingMessageNotificationParams(level="info", data="hello"),
    )


def _updated_uris(outbound: RecordingOutbound) -> list[str]:
    return [str(p["uri"]) for p in outbound.sent("notifications/resources/updated")]


def _real_client() -> Client[Any]:
    return Client("http://127.0.0.1:9999/mcp", mode=CLIENT_CONNECT_MODE)  # disconnected


# ── attach / clone survival ────────────────────────────────────────


class TestAttach:
    def test_installs_our_handler(self) -> None:
        client = _FakeClient()
        attach_resource_forwarding(client, "svg-mcp")
        assert isinstance(client._session_kwargs.get("message_handler"), _ResourceNotifyHandler)

    def test_idempotent_for_the_same_target(self) -> None:
        client = _FakeClient()
        connection, _ = recording_connection()
        attach_resource_forwarding(client, "svg-mcp", target=connection)
        first = client._session_kwargs.get("message_handler")
        attach_resource_forwarding(client, "svg-mcp", target=connection)
        assert client._session_kwargs.get("message_handler") is first  # not re-created

    def test_rebinds_a_handler_for_another_target(self) -> None:
        client = _FakeClient()
        chat_a, _ = recording_connection()
        chat_b, _ = recording_connection()
        attach_resource_forwarding(client, "svg-mcp", target=chat_a)
        attach_resource_forwarding(client, "svg-mcp", target=chat_b)
        handler = client._session_kwargs.get("message_handler")
        assert isinstance(handler, _ResourceNotifyHandler)
        assert handler.targets(chat_b) and not handler.targets(chat_a)

    def test_real_clone_drops_then_is_reattached(self) -> None:
        """Drive FastMCP's actual Client.new() (the isolate per-chat clone): the
        clone comes without our handler, and the factory's re-attach installs
        it bound to the chat. Pins the clone trap against real FastMCP
        behavior, not a simulation."""
        client = _real_client()
        attach_resource_forwarding(client, "svg-mcp")  # base: broadcast

        clone = client.new()  # the real FastMCP clone
        assert not isinstance(clone._session_kwargs.get("message_handler"), _ResourceNotifyHandler)

        chat, _ = recording_connection()
        attach_resource_forwarding(clone, "svg-mcp", target=chat)  # factory re-attach
        handler = clone._session_kwargs.get("message_handler")
        assert isinstance(handler, _ResourceNotifyHandler) and handler.targets(chat)

    def test_fail_loud_when_session_kwargs_missing(self) -> None:
        """If FastMCP restructures _session_kwargs, attach warns and no-ops rather
        than silently dropping forwarding."""

        class _NoKwargs:
            pass

        obj = _NoKwargs()
        # Must not raise; returns the object unchanged (warning logged).
        assert attach_resource_forwarding(obj, "svg-mcp") is obj


# ── forwarding_factory: point-of-use attach (paths 3 & 4 regression) ──


async def _resolve(factory: Any) -> Any:
    client = factory()
    if inspect.isawaitable(client):
        client = await client
    return client


class TestForwardingFactory:
    """Forwarding attached at construction would miss the clients paths 3 & 4
    hand out (a per-request client.new() clone / an internally-built client),
    so the factory is the seam: every handed-out client is attached."""

    async def test_wrapped_factory_output_is_attached(self) -> None:
        wrapped = forwarding_factory(_real_client, "svg-mcp", per_chat=False)
        client = await _resolve(wrapped)
        assert isinstance(client._session_kwargs.get("message_handler"), _ResourceNotifyHandler)

    async def test_fresh_clone_each_call_is_attached(self) -> None:
        wrapped = forwarding_factory(_real_client, "svg-mcp", per_chat=False)
        c1, c2 = await _resolve(wrapped), await _resolve(wrapped)
        assert c1 is not c2  # genuinely fresh each call
        for c in (c1, c2):
            assert isinstance(c._session_kwargs["message_handler"], _ResourceNotifyHandler)

    async def test_async_factory_is_preserved(self) -> None:
        async def afactory() -> Client[Any]:
            return _real_client()

        wrapped = forwarding_factory(afactory, "svg-mcp", per_chat=False)
        client = await _resolve(wrapped)  # wrapper preserves the async factory
        assert isinstance(client._session_kwargs.get("message_handler"), _ResourceNotifyHandler)

    async def test_per_chat_without_context_falls_back_to_broadcast(self) -> None:
        """per_chat=True but no request context → target=None (broadcast); the
        factory must not raise."""
        wrapped = forwarding_factory(_real_client, "svg-mcp", per_chat=True)
        client = await _resolve(wrapped)  # must not raise
        handler = client._session_kwargs.get("message_handler")
        assert isinstance(handler, _ResourceNotifyHandler) and handler.targets(None)

    async def test_real_fastmcp_proxy_via_public_constructor(self) -> None:
        """Pass the wrapped factory via the PUBLIC FastMCPProxy(client_factory=…):
        BOTH the proxy's own factory and its ProxyProvider (which inherits it)
        hand out attached clients — no reach into the built proxy. Pins
        _create_client_factory + FastMCPProxy against a rename."""
        from fastmcp.server.providers.proxy import FastMCPProxy, _create_client_factory

        base = _create_client_factory("http://127.0.0.1:9999/mcp", mode=CLIENT_CONNECT_MODE)
        proxy = FastMCPProxy(
            client_factory=forwarding_factory(base, "svg-mcp", per_chat=False), name="t"
        )

        client = await _resolve(proxy.client_factory)
        assert isinstance(client._session_kwargs.get("message_handler"), _ResourceNotifyHandler)

        provider = next(p for p in proxy.providers if callable(getattr(p, "client_factory", None)))
        pclient = await _resolve(getattr(provider, "client_factory"))  # noqa: B009
        assert isinstance(pclient._session_kwargs.get("message_handler"), _ResourceNotifyHandler)


# ── forward: resource notifications namespaced + routed ────────────


class TestForwarding:
    @pytest.fixture
    def broadcast_targets(self) -> Iterator[tuple[RecordingOutbound, RecordingOutbound]]:
        (c1, o1), (c2, o2) = recording_connection(), recording_connection()
        RUNTIME.sessions.active.add(c1)
        RUNTIME.sessions.active.add(c2)
        yield o1, o2
        RUNTIME.sessions.active.discard(c1)
        RUNTIME.sessions.active.discard(c2)

    async def test_updated_forward_namespaced_to_target(self) -> None:
        """Per-chat: the target connection gets the namespaced URI."""
        target, outbound = recording_connection()
        handler = _ResourceNotifyHandler("svg-mcp", target=target)

        await handler(_updated())

        assert _updated_uris(outbound) == [NAMESPACED]

    async def test_updated_broadcasts_when_no_target(
        self, broadcast_targets: tuple[RecordingOutbound, RecordingOutbound]
    ) -> None:
        o1, o2 = broadcast_targets
        handler = _ResourceNotifyHandler("svg-mcp", target=None)

        await handler(_updated())

        assert _updated_uris(o1) == [NAMESPACED]
        assert _updated_uris(o2) == [NAMESPACED]

    async def test_dead_target_is_skipped_not_broadcast(
        self, broadcast_targets: tuple[RecordingOutbound, RecordingOutbound]
    ) -> None:
        """The target is weakref'd: once the chat's connection is gone the
        handler notifies no one (no broadcast fallback, no crash)."""
        target, _ = recording_connection()
        handler = _ResourceNotifyHandler("svg-mcp", target=target)
        del target
        gc.collect()
        await handler(_updated())  # must not raise
        assert all(_updated_uris(o) == [] for o in broadcast_targets)

    async def test_list_changed_forwarded(self) -> None:
        target, outbound = recording_connection()
        handler = _ResourceNotifyHandler("svg-mcp", target=target)
        await handler(_list_changed())
        assert len(outbound.sent("notifications/resources/list_changed")) == 1

    async def test_tool_list_changed_triggers_reprime_not_forward(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The upstream's own tool ready-edge: routed through the gated prime
        for THIS server (which decides any publication), NOT blind-forwarded
        to the downstream target — so clients are never told to re-fetch into
        a cache whose slice for this server may still be stale."""
        import mcp_combiner.toolcache as toolcache_mod

        reprimed: list[str] = []
        monkeypatch.setattr(toolcache_mod, "_on_upstream_tools_ready", reprimed.append)

        target, outbound = recording_connection()
        handler = _ResourceNotifyHandler("svg-mcp", target=target)
        await handler(_tools_changed())  # full dispatch chain

        assert reprimed == ["svg-mcp"]  # server A's session can only re-prime A
        assert outbound.notifications == []  # no blind downstream forward

    async def test_other_notifications_pass_through(self) -> None:
        """A non-resource notification: no forward, no crash."""
        target, outbound = recording_connection()
        handler = _ResourceNotifyHandler("svg-mcp", target=target)
        await handler(_log())
        assert outbound.notifications == []


class TestListenStreams:
    """2026-era chats have no long-lived Connection: forwarding also publishes
    to the event hub — per chat by token, or broadcast."""

    async def test_per_chat_update_reaches_only_that_chats_bus(self) -> None:
        mine: list[ServerEvent] = []
        theirs: list[ServerEvent] = []
        undo_mine = HUB.chat_bus("chat-mine").subscribe(mine.append)
        undo_theirs = HUB.chat_bus("chat-theirs").subscribe(theirs.append)
        try:
            handler = _ResourceNotifyHandler("svg-mcp", target_token="chat-mine")
            await handler(_updated())
        finally:
            undo_mine()
            undo_theirs()
        assert mine == [ResourceUpdated(uri=NAMESPACED)]
        assert theirs == []

    async def test_broadcast_update_reaches_the_broadcast_bus(self) -> None:
        seen: list[ServerEvent] = []
        undo = HUB.broadcast_bus.subscribe(seen.append)
        try:
            await _ResourceNotifyHandler("svg-mcp")(_list_changed())
        finally:
            undo()
        assert seen == [ResourcesListChanged()]
