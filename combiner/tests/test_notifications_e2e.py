"""In-memory delivery test for resource-notification forwarding (pass 1).

Complements ``test_notifications.py`` (handler + factory unit tests) by driving
the REAL chain end to end, in-process:

    real upstream emits notifications/resources/updated
      → FastMCPProxy(client_factory=forwarding_factory(_create_client_factory(up)))
        ← the code under test, against a REAL FastMCPProxy (pins the public
        client_factory= wiring + _create_client_factory against a rename)
      → the upstream session's message_handler (our _ResourceNotifyHandler)
      → forward-namespaced push to a downstream session

Two things only a live chain proves:

* **wiring** — the wrapped factory really is the one the proxy (and its
  ProxyProvider) hand out per request; a FastMCP change here yields empty
  delivery, not a silently-passing fake.
* **request-scoped delivery** — this is exactly the concern-#1 boundary: the
  emit happens *inside* a tool call, so the per-request upstream session is open
  when it lands. If a proxy path stopped opening a session that hears the
  notification, ``captured`` stays empty and this test localises it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING

import mcp.types
import pytest
from fastmcp import Client, Context, FastMCP
from fastmcp.client.messages import MessageHandler
from fastmcp.server.providers.proxy import FastMCPProxy, _create_client_factory
from fastmcp.server.transforms.namespace import Namespace
from pydantic import AnyUrl

from mcp_combiner.notifications import _ResourceNotifyHandler, forwarding_factory
from mcp_combiner.runtime import RUNTIME


def _forwarding_proxy(upstream: FastMCP, *, per_chat: bool) -> FastMCPProxy:
    """Build the proxy exactly as proxyfactory does: wrap the base factory with
    forwarding_factory and pass it via the public FastMCPProxy(client_factory=…)."""
    base = _create_client_factory(upstream)
    return FastMCPProxy(
        client_factory=forwarding_factory(base, "svg-mcp", per_chat=per_chat), name="svg-mcp"
    )


# _CaptureSession stands in for a real combiner→client ServerSession. Give it that
# type as a base for type-checking only (object at runtime) so it's accepted in
# RUNTIME.sessions' WeakSet[ServerSession] without the real constructor.
if TYPE_CHECKING:
    from mcp.server.session import ServerSession as _SessionBase
else:
    _SessionBase = object

WIDGET_URI = "ui://svg-mcp/preview"
# What the mount's Namespace applies to resources — the handler must push the
# SAME transform so the pushed URI matches resources/list and the tool _meta.
NAMESPACED = str(AnyUrl(Namespace("svg-mcp")._transform_uri(WIDGET_URI)))


class _CaptureSession(_SessionBase):
    """Duck-typed downstream ServerSession: records what gets forwarded to it.

    Only needs the two coroutine methods the handler calls; it stands in for a
    real combiner→client ServerSession in ``RUNTIME.sessions``.
    """

    def __init__(self) -> None:
        self.updated: list[str] = []
        self.list_changed = 0

    async def send_resource_updated(self, uri: AnyUrl) -> None:
        self.updated.append(str(uri))

    async def send_resource_list_changed(self) -> None:
        self.list_changed += 1


def _make_upstream() -> FastMCP:
    """A real upstream that emits resources/updated from a tool (like svg-mcp's
    _emit_change fires after a mutating edit)."""
    up: FastMCP = FastMCP("svg-mcp-upstream")

    @up.resource(WIDGET_URI)
    def widget() -> str:
        return "<html>snapshot</html>"

    @up.tool
    async def edit(ctx: Context) -> str:
        await ctx.session.send_resource_updated(AnyUrl(WIDGET_URI))
        return "edited"

    @up.tool
    async def add_widget(ctx: Context) -> str:
        await ctx.session.send_resource_list_changed()
        return "added"

    return up


class _CaptureHandler(MessageHandler):
    """A downstream client's message handler — records the resource notifications
    the proxy forwards to *this* client (the faithful way to observe per-chat
    routing, since the target is the real ServerSession facing this client)."""

    def __init__(self) -> None:
        super().__init__()
        self.updated: list[str] = []
        self.list_changed = 0

    async def on_resource_updated(self, message: mcp.types.ResourceUpdatedNotification) -> None:
        self.updated.append(str(message.params.uri))

    async def on_resource_list_changed(
        self, message: mcp.types.ResourceListChangedNotification
    ) -> None:
        self.list_changed += 1


@pytest.fixture
def capture() -> Iterator[_CaptureSession]:
    # RUNTIME.sessions.active is a WeakSet — hold the strong ref for the test so
    # the broadcast target isn't GC'd out from under us.
    sess = _CaptureSession()
    RUNTIME.sessions.active.add(sess)  # test double; tests aren't type-checked in CI (see sibling)
    try:
        yield sess
    finally:
        RUNTIME.sessions.active.discard(sess)


async def _settle(pred: Callable[[], bool]) -> None:
    """The notification is emitted before the tool result on the wire, so it is
    usually dispatched by the time call_tool returns; poll briefly to absorb a
    read-loop scheduling race."""
    for _ in range(50):
        if pred():
            return
        await asyncio.sleep(0.02)


def test_forwarding_factory_wraps_a_real_proxy() -> None:
    """Pin the wiring against a real FastMCPProxy: the factory _get_client() uses
    (proxy.client_factory) — set via the public client_factory= param — must hand
    out a client carrying our handler, not the fail-safe no-op."""
    proxy = _forwarding_proxy(_make_upstream(), per_chat=False)

    client = proxy.client_factory()  # the wrapped, real factory
    assert isinstance(client, Client)  # the factory is sync (narrows the union)
    assert isinstance(client._session_kwargs["message_handler"], _ResourceNotifyHandler)


async def test_resource_updated_delivered_namespaced(capture: _CaptureSession) -> None:
    """Full chain: an upstream edit's resources/updated reaches the downstream
    session, forward-namespaced."""
    proxy = _forwarding_proxy(_make_upstream(), per_chat=False)

    async with Client(proxy) as client:
        await client.call_tool("edit")
        await _settle(lambda: bool(capture.updated))

    assert capture.updated == [NAMESPACED], capture.updated


async def test_per_chat_routes_to_requesting_session_only(capture: _CaptureSession) -> None:
    """per_chat=True: the update goes to the requesting chat's own session
    (observed by that client's message handler), and NOT to a bystander session
    in RUNTIME.sessions — proving it's targeted, not broadcast."""
    proxy = _forwarding_proxy(_make_upstream(), per_chat=True)

    handler = _CaptureHandler()
    async with Client(proxy, message_handler=handler) as client:
        await client.call_tool("edit")
        await _settle(lambda: bool(handler.updated))

    assert handler.updated == [NAMESPACED], handler.updated  # this chat got it
    assert capture.updated == []  # the bystander (broadcast target) did NOT


async def test_list_changed_forwarded_through_proxy() -> None:
    """resources/list_changed forwards through a real proxy to the requesting
    chat's client (no URI to namespace — just the signal)."""
    proxy = _forwarding_proxy(_make_upstream(), per_chat=True)

    handler = _CaptureHandler()
    async with Client(proxy, message_handler=handler) as client:
        await client.call_tool("add_widget")
        await _settle(lambda: handler.list_changed > 0)

    assert handler.list_changed == 1
