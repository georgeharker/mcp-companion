"""Forward-namespace upstream resource notifications to downstream clients.

FastMCP's proxy/mount forwards **neither** ``notifications/resources/updated`` nor
``notifications/resources/list_changed`` from an upstream server to the connected
client, and its ``Namespace`` transform would leave the URIs un-namespaced anyway.
So through a namespaced mount an MCP-Apps widget resolves (after the ``_meta`` fix)
but never repaints on edit.

This forwards those notifications, forward-namespacing the URI with the *same*
transform the mount applies to resources (``toolcache.namespace_uri``, pinned
equal to FastMCP's ``Namespace``) — so the pushed URI matches what the host
subscribed to and what ``resources/list`` / the tool ``_meta.ui.resourceUri``
advertise.

Composing with FastMCP's default handler (the important bit)
-----------------------------------------------------------
Every ``Client`` gets ``message_handler or TaskNotificationHandler(self)`` by
default (``fastmcp/client/client.py``); ``TaskNotificationHandler`` routes
``TaskStatusNotification`` to ``client._handle_task_status_notification`` (SEP-1686
background-task delivery).  So we must **not** replace it — we *subclass* it (task
routing inherited) and add the resource hooks (patch-and-pass).

The clone trap (``isolate: true``)
-----------------------------------
``Client.new()`` — used by ``StatefulProxyClient.new_stateful`` for each per-chat
session — re-wraps the handler with a hardcoded *plain* ``TaskNotificationHandler``:

    if isinstance(handler, TaskNotificationHandler):
        new_client._session_kwargs["message_handler"] = TaskNotificationHandler(new_client)

so a subclass is silently downgraded on every clone (exactly the live-repaint case).
The fix is to **re-attach after the clone** by wrapping the client factory (see
``proxyfactory``).  ``new_stateful`` caches per session, so re-attach is idempotent.

Routing: per-chat under ``isolate: true`` (the handler is bound to the owning
downstream session captured at factory time), else broadcast to all active
sessions (a shared upstream can't tell chats apart; the host ignores URIs it
didn't subscribe to).

Scope: forward direction only.  The reverse client→upstream handshake
(advertising ``resources.subscribe``, forwarding ``resources/subscribe`` /
``unsubscribe``) is deferred — FastMCP doesn't surface server-side subscribe today.

Delivery ceiling — "attached" is not "delivered"
------------------------------------------------
The handler is now correctly attached on *all* four proxy paths, but reception
still requires a client session to be **open and listening** when the upstream
emits.  Only the long-lived-session paths deliver reliably:

* ``isolate: true`` (per-chat stateful session) — **reliable** (svg-mcp's case).
* persistent HTTP/SSE connection — **reliable** (one standing session).
* auth / stdio without a persistent connection — a session is opened *per
  request* and closed after, so there is no standing listener between requests.
  An update emitted **inside** a request window (e.g. the edit that triggered it
  is itself a tool call → a session is open) is delivered; one emitted between
  requests is dropped.  So a shared/stateless stdio server's widget repaint is
  timing-dependent, not guaranteed — for guaranteed live repaint the server must
  run ``isolate: true`` (or use a persistent connection).
"""

from __future__ import annotations

import inspect
import logging
import weakref
from collections.abc import Awaitable, Callable
from typing import Any

import mcp.types
from fastmcp.client.tasks import TaskNotificationHandler
from fastmcp.server.dependencies import get_context
from mcp.server.session import ServerSession
from pydantic import AnyUrl

from mcp_combiner.runtime import RUNTIME
from mcp_combiner.toolcache import namespace_uri

logger = logging.getLogger("mcp-combiner")


class _ResourceNotifyHandler(TaskNotificationHandler):
    """A ``TaskNotificationHandler`` (task routing inherited) that also forwards
    resource notifications downstream, forward-namespaced.

    ``target`` is the single downstream session to notify (per-chat under
    ``isolate: true``); ``None`` broadcasts to every active session.
    """

    def __init__(
        self, client: Any, server_name: str, *, target: ServerSession | None = None
    ) -> None:
        super().__init__(client)  # binds the weakref → TaskStatusNotification routing
        self._server = server_name
        # Weakref the per-chat target so the handler never pins the downstream
        # session alive (the session's own exit tears down the per-chat client;
        # a dead ref just means "chat gone → nothing to notify"). None = broadcast.
        self._target_ref: weakref.ref[ServerSession] | None = (
            weakref.ref(target) if target is not None else None
        )

    async def on_resource_updated(
        self, notification: mcp.types.ResourceUpdatedNotification
    ) -> None:
        try:
            uri = AnyUrl(namespace_uri(str(notification.params.uri), self._server))
        except Exception:
            # Namespacing must never raise into the client receive loop — if the
            # transform breaks (FastMCP internals changed), drop the notification
            # rather than disrupt the session.
            logger.debug(
                "Failed to namespace resources/updated URI for '%s'", self._server, exc_info=True
            )
            return
        await self._forward(lambda s: s.send_resource_updated(uri), f"resources/updated {uri}")

    async def on_resource_list_changed(
        self, notification: mcp.types.ResourceListChangedNotification
    ) -> None:
        await self._forward(
            lambda s: s.send_resource_list_changed(), f"resources/list_changed ({self._server})"
        )

    async def on_tool_list_changed(
        self, notification: mcp.types.ToolListChangedNotification
    ) -> None:
        """The upstream's own tool ready-edge: re-prime THIS server only.

        Not a blind downstream forward — that would invite clients to re-fetch
        into a cache whose slice for this server may still be stale. The shared
        prime path re-lists through the mounted provider, records the outcome
        in the confirmed-tools store, and publishes exactly once iff the slice
        was newly confirmed or replaced by different content (an upstream
        that warmed up from an empty boot answer converges here).

        ``self._server`` is closed over per handler instance, so server A's
        upstream session can only ever re-prime A. The per-chat ``target`` is
        deliberately ignored: tool slices are combiner-global, so any needed
        publication is a broadcast decided by the gated prime.
        """
        try:
            from mcp_combiner.toolcache import _on_upstream_tools_ready

            logger.info("tools/list_changed from upstream '%s' — re-priming", self._server)
            _on_upstream_tools_ready(self._server)
        except Exception:
            # Never raise into the client receive loop.
            logger.debug(
                "Failed to handle tools/list_changed for '%s'", self._server, exc_info=True
            )

    async def _forward(self, send: Callable[[ServerSession], Awaitable[None]], desc: str) -> None:
        if self._target_ref is not None:
            # Per-chat: notify only this chat's session (skip if it's gone).
            target = self._target_ref()
            sessions: list[ServerSession] = [] if target is None else [target]
        else:
            # Broadcast: shared upstream serves every chat.
            sessions = RUNTIME.sessions.sessions()
        for session in sessions:
            if session is None:
                continue
            try:
                await send(session)
            except Exception:
                logger.debug(
                    "Failed to forward %s (%s) to a session", desc, self._server, exc_info=True
                )


def attach_resource_forwarding(
    client: Any, server_name: str, *, target: ServerSession | None = None
) -> Any:
    """Install (or re-install) resource-forwarding on *client*'s message handler.

    Call after construction, before connect — the handler needs the client ref,
    and ``Client(message_handler=…)`` is a chicken/egg, so we set it on
    ``_session_kwargs`` exactly the way FastMCP's own clone code does.

    Idempotent: skips if our handler is already installed.  Re-installs over the
    *plain* ``TaskNotificationHandler`` that ``Client.new()`` leaves after a
    per-chat clone (undoing the downgrade).

    We set ``_session_kwargs["message_handler"]`` directly — the *same* field and
    mechanism FastMCP's own clone code uses (``Client.new``).  It's a private
    attribute, so if a FastMCP upgrade restructures it we **warn loudly** and
    skip rather than silently drop forwarding.

    **Fail-safe**: this runs inside the client factory on the tool-call path, so
    it must never raise — any failure (missing ``_session_kwargs``, a changed
    ``TaskNotificationHandler``/``Namespace`` constructor) degrades to "no
    forwarding + warning", never a broken tool call.  The client keeps whatever
    handler FastMCP installed, so task routing is unaffected.
    """
    try:
        kwargs = getattr(client, "_session_kwargs", None)
        if not isinstance(kwargs, dict) or "message_handler" not in kwargs:
            logger.warning(
                "Cannot attach resource forwarding for '%s': FastMCP client has no "
                "_session_kwargs['message_handler'] (internals changed?) — resource "
                "notifications will NOT be forwarded for this server.",
                server_name,
            )
            return client
        if isinstance(kwargs["message_handler"], _ResourceNotifyHandler):
            return client
        kwargs["message_handler"] = _ResourceNotifyHandler(client, server_name, target=target)
    except Exception:
        logger.warning(
            "Failed to attach resource forwarding for '%s' (FastMCP internals "
            "changed?) — resource notifications will NOT be forwarded; tool calls "
            "are unaffected.",
            server_name,
            exc_info=True,
        )
    return client


def forwarding_factory(factory: Any, server_name: str, *, per_chat: bool) -> Any:
    """Wrap a client factory so forwarding is (re)attached at *point of use* — to
    whatever client the factory is about to hand out — then pass the result to
    ``FastMCPProxy(client_factory=…)`` (the public constructor param).  Because
    ``FastMCPProxy`` shares one factory with its ``ProxyProvider``, wrapping here
    covers both without ever reaching into the built proxy's attributes.

    This is the crux: three of four proxy paths obtain their working client via a
    per-request ``client.new()`` clone (which downgrades our handler) or an
    internally-built client (never attached at construction).  Attaching here —
    right before the client is used — survives all of them.  ``attach_*`` is
    idempotent, so a reused/persistent client is a no-op.

    ``per_chat`` captures the requesting downstream session for per-chat routing
    (``isolate: true``); a missing context yields ``None`` (broadcast) — the
    factory must never raise.
    """

    def _target() -> ServerSession | None:
        if not per_chat:
            return None
        try:
            return get_context().session
        except Exception:
            # An isolate server should always have a request context here; if it
            # doesn't, degrade to broadcast rather than raise — but make the
            # degradation visible instead of silent.
            logger.debug(
                "No request context resolving per-chat target for '%s'; falling back to broadcast",
                server_name,
            )
            return None

    def wrapped() -> Any:
        client = factory()
        if inspect.isawaitable(client):

            async def _finish() -> Any:
                resolved = await client
                return attach_resource_forwarding(resolved, server_name, target=_target())

            return _finish()
        return attach_resource_forwarding(client, server_name, target=_target())

    return wrapped
