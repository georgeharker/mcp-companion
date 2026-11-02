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

Composing with FastMCP's handler
--------------------------------
A ``Client``'s ``message_handler`` is a plain ``MessageHandler`` slot (fastmcp 4
moved task-status routing into a client *extension*, so nothing else lives in
the handler); we install a ``MessageHandler`` subclass with the resource hooks.

The clone trap (``isolate: true``)
-----------------------------------
``Client.new()`` — used by ``StatefulProxyClient.new_stateful`` for each per-chat
session — rebuilds the clone's handler from the one the parent was
*constructed* with, so a handler installed after construction (ours) does not
survive the clone. The fix is to **re-attach after the clone** by wrapping the
client factory (see ``proxyfactory``), binding the handler to the clone's own
downstream connection; re-attaching with the same target is a no-op, so
``new_stateful``'s per-session caching stays idempotent.

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
from fastmcp import Client
from fastmcp.client.messages import MessageHandler
from fastmcp.server.dependencies import get_context
from mcp.server.connection import Connection
from mcp.server.session import ServerSession
from pydantic import AnyUrl

from mcp_combiner.runtime import DOWNSTREAM_REQUEST, RUNTIME, downstream_connection
from mcp_combiner.toolcache import namespace_uri
from mcp_combiner.upstream_session import use_tolerant_session

logger = logging.getLogger("mcp-combiner")


class _ResourceNotifyHandler(MessageHandler):
    """Forwards upstream resource notifications downstream, forward-namespaced.

    ``target`` is the single downstream session to notify (per-chat under
    ``isolate: true``); ``None`` broadcasts to every active session.
    """

    def __init__(self, server_name: str, *, target: Connection | None = None) -> None:
        super().__init__()
        self._server = server_name
        # Weakref the per-chat target so the handler never pins the downstream
        # session alive (the session's own exit tears down the per-chat client;
        # a dead ref just means "chat gone → nothing to notify"). None = broadcast.
        self._target_ref: weakref.ref[Connection] | None = (
            weakref.ref(target) if target is not None else None
        )

    def targets(self, target: Connection | None) -> bool:
        """Whether this handler already delivers to *target* (None = broadcast)."""
        if self._target_ref is None:
            return target is None
        return target is not None and self._target_ref() is target

    async def on_resource_updated(self, message: mcp.types.ResourceUpdatedNotification) -> None:
        try:
            uri = AnyUrl(namespace_uri(str(message.params.uri), self._server))
        except Exception:
            # Namespacing must never raise into the client receive loop — if the
            # transform breaks (FastMCP internals changed), drop the notification
            # rather than disrupt the session.
            logger.debug(
                "Failed to namespace resources/updated URI for '%s'", self._server, exc_info=True
            )
            return
        await self._forward(lambda c: c.send_resource_updated(str(uri)), f"resources/updated {uri}")

    async def on_resource_list_changed(
        self, message: mcp.types.ResourceListChangedNotification
    ) -> None:
        await self._forward(
            lambda c: c.send_resource_list_changed(), f"resources/list_changed ({self._server})"
        )

    async def on_tool_list_changed(self, message: mcp.types.ToolListChangedNotification) -> None:
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

    async def _forward(self, send: Callable[[Connection], Awaitable[None]], desc: str) -> None:
        if self._target_ref is not None:
            # Per-chat: notify only this chat's connection (skip if it's gone).
            target = self._target_ref()
            connections: list[Connection] = [] if target is None else [target]
        else:
            # Broadcast: shared upstream serves every chat.
            connections = RUNTIME.sessions.connections()
        for connection in connections:
            try:
                await send(connection)
            except Exception:
                logger.debug(
                    "Failed to forward %s (%s) to a session", desc, self._server, exc_info=True
                )


def attach_resource_forwarding(
    client: Any, server_name: str, *, target: Connection | None = None
) -> Any:
    """Install (or re-install) resource-forwarding on *client*'s message handler.

    Call after construction (or after a per-chat clone), before connect.

    Idempotent: skips if our handler is already installed *for the same
    target*, and rebinds one bound to another target. Re-installs on a per-chat
    clone, which ``Client.new()`` builds without it.

    We set ``_session_kwargs["message_handler"]`` directly — the *same* field and
    mechanism FastMCP's own clone code uses (``Client.new``).  It's a private
    attribute, so if a FastMCP upgrade restructures it we **warn loudly** and
    skip rather than silently drop forwarding.

    **Fail-safe**: this runs inside the client factory on the tool-call path, so
    it must never raise — any failure (missing ``_session_kwargs``, a changed
    ``Namespace`` constructor) degrades to "no forwarding + warning", never a
    broken tool call.
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
        current = kwargs["message_handler"]
        if isinstance(current, _ResourceNotifyHandler) and current.targets(target):
            return client
        kwargs["message_handler"] = _ResourceNotifyHandler(server_name, target=target)
    except Exception:
        logger.warning(
            "Failed to attach resource forwarding for '%s' (FastMCP internals "
            "changed?) — resource notifications will NOT be forwarded; tool calls "
            "are unaffected.",
            server_name,
            exc_info=True,
        )
    return client


def _resolve_elicitation_session(
    captured_session: ServerSession | None, captured_request_id: Any
) -> tuple[ServerSession | None, Any]:
    """Pick the downstream session an upstream elicit should surface on: the
    attach-time capture first (precise — bound inside the request task), then the
    middleware-published task-scoped var (covers receive tasks whose context copy
    happened inside a request but before a capture), else None (caller falls back
    to fastmcp's dynamic resolver)."""
    if captured_session is not None:
        return captured_session, captured_request_id
    snap = DOWNSTREAM_REQUEST.get()
    return snap if snap is not None else (None, None)


def _capture_downstream_request() -> tuple[ServerSession | None, Any]:
    try:
        # OUR OWN task-scoped value, set by ToolProcessingMiddleware.on_request —
        # readable even where fastmcp's private server ctxvar is not.
        snap = DOWNSTREAM_REQUEST.get()
        if snap is not None:
            logger.info(
                "elicit capture (middleware ctxvar): session=%s request_id=%s",
                "yes" if snap[0] is not None else "NO",
                snap[1],
            )
            return snap
        ctx = get_context()
        snap = (ctx.session, ctx.request_id)
        logger.info(
            "elicit capture (fastmcp ctxvar): session=%s request_id=%s",
            "yes" if snap[0] is not None else "NO",
            snap[1],
        )
        return snap
    except Exception as exc:
        logger.info("elicit capture FAILED (no downstream request ctxvar active): %s", exc)
        return None, None


def attach_elicitation_forwarding(
    client: Any,
    server_name: str,
    *,
    session: ServerSession | None = None,
    request_id: Any = None,
) -> Any:
    """NEUTRALIZED (2026-10-01, elicit e2e debugging): this override was STEPPING ON
    FastMCP's own elicitation-forwarding machinery — ``StatefulProxyClient`` already
    wraps the default proxy elicit handler with a context-RESTORING wrapper fed by
    ``ProxyTool.run``'s ``_proxy_rc_ref`` stash (see fastmcp
    server/providers/proxy.py: receive-loop tasks inherit STALE ``request_ctx``
    snapshots on stateful clients, and fastmcp's wrapper restores the stash before
    forwarding — which is exactly the problem we tried to solve here with
    contextvars).

    Writing ``_session_kwargs["elicitation_callback"]`` here replaced fastmcp's
    restoring wrapper with a contextvar-flavored one that CANNOT work on
    boot-connected persistent sessions (no downstream request ctxvar in the receive
    task) — the direct cause of the empty/'session is not available' elicit errors.

    The elicit-forwarding requirements on the combiner are therefore:
      1. upstream clients MUST be ``StatefulProxyClient`` instances (``isolate: true``,
         per chat) so the restoring machinery + rc-stash is active, AND
      2. the stash must fire (``ProxyTool.run`` runs on proxy-provider tool calls).
    Keep this function as a documented no-op; revisit only if fastmcp changes.
    """
    _ = server_name, session, request_id
    return client


def _prepare_session(client: object) -> None:
    """Give a handed-out upstream client the combiner's session class (fastmcp's
    proxy factories set their own when they build or clone a client)."""
    if isinstance(client, Client):
        use_tolerant_session(client)


def forwarding_factory(factory: Any, server_name: str, *, per_chat: bool) -> Any:
    """Wrap a client factory so forwarding is (re)attached at *point of use* — to
    whatever client the factory is about to hand out — then pass the result to
    ``FastMCPProxy(client_factory=…)`` (the public constructor param).  Because
    ``FastMCPProxy`` shares one factory with its ``ProxyProvider``, wrapping here
    covers both without ever reaching into the built proxy's attributes.

    This is the crux: three of four proxy paths obtain their working client via a
    per-request ``client.new()`` clone (built without our handler) or an
    internally-built client (never attached at construction).  Attaching here —
    right before the client is used — survives all of them.  ``attach_*`` is
    idempotent, so a reused/persistent client is a no-op.

    ``per_chat`` captures the requesting downstream connection for per-chat routing
    (``isolate: true``); a missing context yields ``None`` (broadcast) — the
    factory must never raise.
    """

    def _target() -> Connection | None:
        if not per_chat:
            return None
        try:
            return downstream_connection(get_context().session)
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
        # Capture the in-flight downstream request NOW — the tool-call request
        # context is active here, and this is the only place the elicitation
        # handler can learn which downstream session (and which related request
        # id) an upstream elicit belongs to. The elicit itself arrives later, on
        # the upstream client's receive task, where this contextvar is gone.
        elicit_session, elicit_request_id = _capture_downstream_request()
        client = factory()
        if inspect.isawaitable(client):

            async def _finish() -> Any:
                resolved = await client
                _prepare_session(resolved)
                attach_elicitation_forwarding(
                    resolved, server_name, session=elicit_session, request_id=elicit_request_id
                )
                return attach_resource_forwarding(resolved, server_name, target=_target())

            return _finish()
        _prepare_session(client)
        attach_elicitation_forwarding(
            client, server_name, session=elicit_session, request_id=elicit_request_id
        )
        return attach_resource_forwarding(client, server_name, target=_target())

    return wrapped
