"""Forward upstream resource notifications to downstream clients, namespaced.

FastMCP's proxy/mount forwards **neither** ``notifications/resources/updated`` nor
``notifications/resources/list_changed`` from an upstream server to the connected
client, and its ``Namespace`` transform would leave the URIs un-namespaced anyway.
This attaches a :class:`~fastmcp.client.messages.MessageHandler` to each upstream
client that catches those notifications, forward-namespaces the URI with the *same*
``Namespace._transform_uri`` the mount applies to resources (so it matches what the
client subscribed to and what ``resources/list`` / the tool ``_meta.ui.resourceUri``
advertise), and pushes them out to the active downstream sessions — so a host
displaying an MCP-Apps widget repaints live on every upstream edit.

**Scope — pass 1, forward direction only.**  Updates are *broadcast* to all active
downstream sessions.  Under ``isolate: true`` each chat has its own upstream
document at the *same* namespaced URI, so a broadcast makes non-editing chats do a
harmless spurious re-read (each re-reads its *own* session's document — content is
always correct; only the editing chat sees a real change).  Precise
per-subscription routing — plus the reverse client→upstream handshake (advertising
``resources.subscribe`` and forwarding ``resources/subscribe``/``unsubscribe``) —
is the pass-2 refinement; FastMCP does not surface subscribe server-side today.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import mcp.types
from fastmcp.client.messages import MessageHandler
from fastmcp.server.transforms.namespace import Namespace
from mcp.server.session import ServerSession
from pydantic import AnyUrl

from mcp_combiner.runtime import RUNTIME

logger = logging.getLogger("mcp-combiner")


async def _broadcast(send: Callable[[ServerSession], Awaitable[None]], desc: str) -> None:
    """Run *send* against every active downstream session, swallowing per-session
    errors (a dead session must never block the rest — mirrors the tools/list_changed
    broadcast)."""
    sessions = RUNTIME.sessions.sessions()
    for session in sessions:
        try:
            await send(session)
        except Exception:
            logger.debug("Failed to forward %s to a session", desc, exc_info=True)
    if sessions:
        logger.debug("Forwarded %s to %d downstream session(s)", desc, len(sessions))


class _ResourceNotifyHandler(MessageHandler):
    """Forward-namespaces one upstream server's resource notifications downstream.

    Bound to a single server's namespace, so it can be shared across that server's
    per-chat sessions (the notification URI is server-scoped, not chat-scoped).
    """

    def __init__(self, server_name: str) -> None:
        self._server = server_name
        self._ns = Namespace(server_name)

    async def on_resource_updated(
        self, notification: mcp.types.ResourceUpdatedNotification
    ) -> None:
        namespaced = self._ns._transform_uri(str(notification.params.uri))
        uri = AnyUrl(namespaced)
        await _broadcast(
            lambda s: s.send_resource_updated(uri), f"resources/updated {namespaced}"
        )

    async def on_resource_list_changed(
        self, notification: mcp.types.ResourceListChangedNotification
    ) -> None:
        await _broadcast(
            lambda s: s.send_resource_list_changed(), f"resources/list_changed ({self._server})"
        )


def resource_notify_handler(server_name: str) -> MessageHandler:
    """A ``message_handler`` that forward-namespaces *server_name*'s resource
    notifications to downstream clients.

    Pass as ``message_handler=`` when constructing the upstream ``Client`` /
    ``StatefulProxyClient``.  It observes notifications only (dispatched via
    ``MessageHandler.on_*`` hooks) and does not interfere with the proxy's
    sampling / elicitation / roots callbacks, which are separate kwargs.
    """
    return _ResourceNotifyHandler(server_name)
