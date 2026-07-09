"""Forward-namespacing of upstream resource notifications (pass 1, forward-only).

An upstream ``resources/updated`` / ``resources/list_changed`` is caught by the
per-server MessageHandler, its URI forward-namespaced with the same transform the
mount applies to resources, and broadcast to active downstream sessions so a host
displaying an MCP-Apps widget repaints live.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastmcp.server.transforms.namespace import Namespace
from mcp.types import (
    ResourceListChangedNotification,
    ResourceUpdatedNotification,
    ResourceUpdatedNotificationParams,
)
from pydantic import AnyUrl

from mcp_combiner.notifications import resource_notify_handler
from mcp_combiner.runtime import RUNTIME


class _FakeSession:
    """Stand-in downstream ServerSession recording what it was sent."""

    def __init__(self) -> None:
        self.updated: list[str] = []
        self.list_changed = 0

    async def send_resource_updated(self, uri: AnyUrl) -> None:
        self.updated.append(str(uri))

    async def send_resource_list_changed(self) -> None:
        self.list_changed += 1


@pytest.fixture
def sessions() -> Iterator[tuple[_FakeSession, _FakeSession]]:
    s1, s2 = _FakeSession(), _FakeSession()
    RUNTIME.sessions.active.add(s1)
    RUNTIME.sessions.active.add(s2)
    yield s1, s2
    RUNTIME.sessions.active.discard(s1)
    RUNTIME.sessions.active.discard(s2)


def _updated(uri: str) -> ResourceUpdatedNotification:
    return ResourceUpdatedNotification(
        method="notifications/resources/updated",
        params=ResourceUpdatedNotificationParams(uri=AnyUrl(uri)),
    )


class TestResourceNotifyHandler:
    async def test_updated_is_forward_namespaced_and_broadcast(
        self, sessions: tuple[_FakeSession, _FakeSession]
    ) -> None:
        s1, s2 = sessions
        handler = resource_notify_handler("svg-mcp")

        await handler.on_resource_updated(_updated("ui://svg-mcp/preview"))

        # Namespaced with the SAME transform resources/list uses, so the pushed
        # URI matches what the host subscribed to.
        expected = str(AnyUrl(Namespace("svg-mcp")._transform_uri("ui://svg-mcp/preview")))
        assert s1.updated == [expected]
        assert s2.updated == [expected]  # broadcast to every active session

    async def test_namespaced_uri_reverse_resolves_upstream(
        self, sessions: tuple[_FakeSession, _FakeSession]
    ) -> None:
        s1, _ = sessions
        handler = resource_notify_handler("svg-mcp")
        await handler.on_resource_updated(_updated("ui://svg-mcp/preview"))
        # The forwarded URI, run back through the mount's reverse, lands on the
        # real upstream URI — i.e. read/subscribe stay consistent with the notify.
        sent = s1.updated[0].rstrip("/")
        assert Namespace("svg-mcp")._reverse_uri(sent) in (
            "ui://svg-mcp/preview",
            "ui://svg-mcp/preview/",
        )

    async def test_list_changed_broadcast(
        self, sessions: tuple[_FakeSession, _FakeSession]
    ) -> None:
        s1, s2 = sessions
        handler = resource_notify_handler("svg-mcp")
        await handler.on_resource_list_changed(
            ResourceListChangedNotification(method="notifications/resources/list_changed")
        )
        assert s1.list_changed == 1
        assert s2.list_changed == 1

    async def test_per_session_error_does_not_block_others(
        self, sessions: tuple[_FakeSession, _FakeSession]
    ) -> None:
        s1, s2 = sessions

        async def boom(uri: AnyUrl) -> None:
            raise RuntimeError("dead session")

        s1.send_resource_updated = boom  # type: ignore[method-assign]
        handler = resource_notify_handler("svg-mcp")

        # Must not raise, and s2 still gets it despite s1 failing.
        await handler.on_resource_updated(_updated("ui://svg-mcp/preview"))
        assert len(s2.updated) == 1
