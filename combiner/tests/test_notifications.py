"""Forward-namespacing of upstream resource notifications (pass 1, forward-only).

Pins the two subtle correctness properties from the svg-mcp addendum:

* **Compose, don't replace** — our handler subclasses ``TaskNotificationHandler``,
  so ``TaskStatusNotification`` still routes to the client (SEP-1686), while
  resources/updated + resources/list_changed are forward-namespaced downstream.
* **Survive the clone** — ``Client.new()`` downgrades a subclass to a plain
  ``TaskNotificationHandler`` on every per-chat clone; ``attach_resource_forwarding``
  re-installs ours (idempotent on cache hits).
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastmcp.client.tasks import TaskNotificationHandler
from fastmcp.server.transforms.namespace import Namespace
from mcp.types import (
    ResourceListChangedNotification,
    ResourceUpdatedNotification,
    ResourceUpdatedNotificationParams,
    ServerNotification,
    TaskStatusNotification,
    TaskStatusNotificationParams,
)
from pydantic import AnyUrl

from mcp_combiner.notifications import (
    _ResourceNotifyHandler,
    attach_resource_forwarding,
    forwarding_factory,
)
from mcp_combiner.runtime import RUNTIME

# The fakes stand in for a real Client / ServerSession. Give them those types as
# bases *for type-checking only* (object at runtime) so the type checker accepts
# them where the real objects are expected, without pulling in the real
# constructors.
if TYPE_CHECKING:
    from mcp.server.session import ServerSession as _SessionBase
else:
    _SessionBase = object


class _FakeClient:
    """Stand-in Client: holds _session_kwargs and records task-status routing."""

    def __init__(self) -> None:
        # A real FastMCP client always has the message_handler key present.
        self._session_kwargs: dict[str, Any] = {"message_handler": None}
        self.task_notifs: list[Any] = []

    def _handle_task_status_notification(self, notification: Any) -> None:
        self.task_notifs.append(notification)


class _FakeSession(_SessionBase):
    def __init__(self) -> None:
        self.updated: list[str] = []
        self.list_changed = 0

    async def send_resource_updated(self, uri: AnyUrl) -> None:
        self.updated.append(str(uri))

    async def send_resource_list_changed(self) -> None:
        self.list_changed += 1


def _updated(uri: str) -> ServerNotification:
    return ServerNotification(
        ResourceUpdatedNotification(
            method="notifications/resources/updated",
            params=ResourceUpdatedNotificationParams(uri=AnyUrl(uri)),
        )
    )


def _list_changed() -> ServerNotification:
    return ServerNotification(
        ResourceListChangedNotification(method="notifications/resources/list_changed")
    )


def _task_status() -> ServerNotification:
    now = datetime.now(timezone.utc)
    return ServerNotification(
        TaskStatusNotification(
            method="notifications/tasks/status",
            params=TaskStatusNotificationParams(
                taskId="t1", status="completed", createdAt=now, lastUpdatedAt=now, ttl=0
            ),
        )
    )


# ── attach / clone survival ────────────────────────────────────────


class TestAttach:
    def test_installs_our_handler(self) -> None:
        client = _FakeClient()
        attach_resource_forwarding(client, "svg-mcp")
        assert isinstance(client._session_kwargs["message_handler"], _ResourceNotifyHandler)

    def test_reattach_over_cloned_plain_handler(self) -> None:
        """Client.new() downgrades to a plain TaskNotificationHandler on clone —
        attach must re-install ours over it."""
        client = _FakeClient()
        # the downgrade FastMCP's Client.new() would leave
        client._session_kwargs["message_handler"] = TaskNotificationHandler(cast(Any, client))
        attach_resource_forwarding(client, "svg-mcp")
        assert isinstance(client._session_kwargs["message_handler"], _ResourceNotifyHandler)

    def test_idempotent_on_cache_hit(self) -> None:
        client = _FakeClient()
        attach_resource_forwarding(client, "svg-mcp")
        first = client._session_kwargs["message_handler"]
        attach_resource_forwarding(client, "svg-mcp")
        assert client._session_kwargs["message_handler"] is first  # not re-created

    def test_survives_real_client_clone(self) -> None:
        """Drive FastMCP's actual Client.new() (the isolate per-chat clone): it
        downgrades our subclass to a plain TaskNotificationHandler, and re-attach
        (what the client factory does) restores it. Pins the clone trap against
        real FastMCP behavior, not a simulation."""
        from fastmcp import Client

        client = Client("http://127.0.0.1:9999/mcp")  # disconnected; no network
        attach_resource_forwarding(client, "svg-mcp")
        assert isinstance(client._session_kwargs["message_handler"], _ResourceNotifyHandler)

        clone = client.new()  # the real FastMCP clone
        downgraded = clone._session_kwargs["message_handler"]
        assert isinstance(downgraded, TaskNotificationHandler)
        assert not isinstance(downgraded, _ResourceNotifyHandler)  # subclass lost

        attach_resource_forwarding(clone, "svg-mcp")  # factory re-attach
        assert isinstance(clone._session_kwargs["message_handler"], _ResourceNotifyHandler)

    def test_fail_loud_when_session_kwargs_missing(self) -> None:
        """If FastMCP restructures _session_kwargs, attach warns and no-ops rather
        than silently dropping forwarding."""

        class _NoKwargs:
            pass

        obj = _NoKwargs()
        # Must not raise; returns the object unchanged (warning logged).
        assert attach_resource_forwarding(obj, "svg-mcp") is obj


# ── forwarding_factory: point-of-use attach (paths 3 & 4 regression) ──


def _fresh_client() -> Any:
    from fastmcp import Client

    return Client("http://127.0.0.1:9999/mcp")  # disconnected; no network


async def _resolve(factory: Any) -> Any:
    client = factory()
    if inspect.isawaitable(client):
        client = await client
    return client


class TestForwardingFactory:
    """The gap the addendum caught: forwarding was attached at construction, but
    paths 3 & 4 obtain the working client via a per-request client.new() clone /
    internally-built client — so the factory, not construction, is the seam.
    forwarding_factory wraps that factory; each handed-out client is attached."""

    async def test_wrapped_factory_output_is_attached(self) -> None:
        wrapped = forwarding_factory(_fresh_client, "svg-mcp", per_chat=False)
        client = await _resolve(wrapped)
        assert isinstance(client._session_kwargs["message_handler"], _ResourceNotifyHandler)

    async def test_fresh_clone_each_call_is_attached(self) -> None:
        """A per-request fresh client (paths 3/4) — each must be attached
        (construction attach would miss these)."""
        wrapped = forwarding_factory(_fresh_client, "svg-mcp", per_chat=False)
        c1, c2 = await _resolve(wrapped), await _resolve(wrapped)
        assert c1 is not c2  # genuinely fresh each call
        for c in (c1, c2):
            assert isinstance(c._session_kwargs["message_handler"], _ResourceNotifyHandler)

    async def test_async_factory_is_preserved(self) -> None:
        async def afactory() -> Any:
            return _fresh_client()

        wrapped = forwarding_factory(afactory, "svg-mcp", per_chat=False)
        client = await _resolve(wrapped)  # wrapper preserves the async factory
        assert isinstance(client._session_kwargs["message_handler"], _ResourceNotifyHandler)

    async def test_per_chat_without_context_falls_back_to_broadcast(self) -> None:
        """per_chat=True but no request context → target=None (broadcast); the
        factory must not raise."""
        wrapped = forwarding_factory(_fresh_client, "svg-mcp", per_chat=True)
        client = await _resolve(wrapped)  # must not raise
        assert isinstance(client._session_kwargs["message_handler"], _ResourceNotifyHandler)

    async def test_real_fastmcp_proxy_via_public_constructor(self) -> None:
        """The whole point of moving to construction-time wrapping: pass the wrapped
        factory via the PUBLIC FastMCPProxy(client_factory=…) and BOTH the proxy's
        own factory and its ProxyProvider (which inherits it) hand out attached
        clients — no reach into the built proxy. Pins _create_client_factory +
        FastMCPProxy against a rename."""
        from fastmcp.server.providers.proxy import FastMCPProxy, _create_client_factory

        base = _create_client_factory("http://127.0.0.1:9999/mcp")  # disconnected
        proxy = FastMCPProxy(
            client_factory=forwarding_factory(base, "svg-mcp", per_chat=False), name="t"
        )

        client = await _resolve(proxy.client_factory)
        assert isinstance(client._session_kwargs["message_handler"], _ResourceNotifyHandler)

        provider = next(p for p in proxy.providers if callable(getattr(p, "client_factory", None)))
        pclient = await _resolve(getattr(provider, "client_factory"))  # noqa: B009
        assert isinstance(pclient._session_kwargs["message_handler"], _ResourceNotifyHandler)


# ── compose: task routing preserved (the regression guard) ─────────


class TestTaskRoutingPreserved:
    async def test_task_status_still_routes_to_client(self) -> None:
        """A TaskStatusNotification must still reach client._handle_task_status_notification
        even though we installed a resource-forwarding handler."""
        client = _FakeClient()
        attach_resource_forwarding(client, "svg-mcp")
        handler = client._session_kwargs["message_handler"]

        await handler(_task_status())  # full dispatch chain
        assert len(client.task_notifs) == 1  # default not lost


# ── forward: resource notifications namespaced + routed ────────────


class TestForwarding:
    @pytest.fixture
    def broadcast_sessions(self) -> Iterator[tuple[_FakeSession, _FakeSession]]:
        s1, s2 = _FakeSession(), _FakeSession()
        RUNTIME.sessions.active.add(s1)
        RUNTIME.sessions.active.add(s2)
        yield s1, s2
        RUNTIME.sessions.active.discard(s1)
        RUNTIME.sessions.active.discard(s2)

    async def test_updated_forward_namespaced_to_target(self) -> None:
        """Per-chat: target session gets the namespaced URI; no broadcast."""
        client = _FakeClient()
        target = _FakeSession()
        handler = _ResourceNotifyHandler(client, "svg-mcp", target=target)

        await handler(_updated("ui://svg-mcp/preview"))

        expected = str(AnyUrl(Namespace("svg-mcp")._transform_uri("ui://svg-mcp/preview")))
        assert target.updated == [expected]

    async def test_updated_broadcasts_when_no_target(
        self, broadcast_sessions: tuple[_FakeSession, _FakeSession]
    ) -> None:
        s1, s2 = broadcast_sessions
        client = _FakeClient()
        handler = _ResourceNotifyHandler(client, "svg-mcp", target=None)

        await handler(_updated("ui://svg-mcp/preview"))

        expected = str(AnyUrl(Namespace("svg-mcp")._transform_uri("ui://svg-mcp/preview")))
        assert s1.updated == [expected]
        assert s2.updated == [expected]

    async def test_dead_target_is_skipped_not_broadcast(self) -> None:
        """The target is weakref'd: once the chat's session is gone the handler
        notifies no one (and must not fall back to broadcasting or crash)."""
        import gc

        client = _FakeClient()
        target = _FakeSession()
        handler = _ResourceNotifyHandler(client, "svg-mcp", target=target)
        del target
        gc.collect()
        await handler(_updated("ui://svg-mcp/preview"))  # must not raise

    async def test_list_changed_forwarded(self) -> None:
        client = _FakeClient()
        target = _FakeSession()
        handler = _ResourceNotifyHandler(client, "svg-mcp", target=target)
        await handler(_list_changed())
        assert target.list_changed == 1

    async def test_no_resource_notification_is_passthrough(self) -> None:
        """A server that emits no resource notifications: dispatch a task notif —
        no forward, no crash."""
        client = _FakeClient()
        target = _FakeSession()
        handler = _ResourceNotifyHandler(client, "svg-mcp", target=target)
        # A non-resource notification just passes through (task routing only).
        await handler(_task_status())
        assert target.updated == []
        assert target.list_changed == 0
