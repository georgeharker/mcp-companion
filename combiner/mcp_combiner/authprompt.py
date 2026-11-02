"""Out-of-band OAuth authorization: hand the authorization URL to the user.

When an OAuth upstream needs (re-)authorization and its prompt mode
(``auth.oauth.prompt``, see :data:`~mcp_combiner.config.AuthPrompt`) is not
plain ``"browser"``, the auth flow does not block the request that hit the 401
while it waits for the user. Instead it publishes a :class:`PendingAuthorization`
here and raises :class:`AuthorizationPending`:

* the request fails at once, before the upstream ran it, so a tool call is never
  replayed minutes later after the user authorizes;
* tool calls routed to the server see the pending authorization (middleware) and
  fail with a URL-mode elicitation error (``-32042``) carrying the URL, which a
  client shows to the user and never to the model;
* the OAuth redirect comes back to ``/oauth/callback/<server>`` on the combiner
  (``oauth.public_url`` set) or to a loopback callback server, and
  :func:`complete` exchanges the code, re-arms the connection via the
  registered :func:`set_on_authorized` hook and notifies the sessions that were
  told (``notifications/elicitation/complete``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import sys
import time
import uuid
import weakref
import webbrowser
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from fastmcp.exceptions import ToolError
from mcp.client.auth.exceptions import OAuthFlowError
from mcp.server.session import ServerSession
from mcp.shared.exceptions import UrlElicitationRequiredError
from mcp.types import ElicitRequestURLParams

logger = logging.getLogger("mcp-combiner")

#: Exchanges an authorization code for tokens and stores them.
CodeExchange = Callable[[str], Awaitable[None]]


@dataclass
class PendingAuthorization:
    """One outstanding authorization the user has to complete."""

    server: str
    url: str
    state: str
    exchange: CodeExchange
    expires_at: float
    #: hand the URL to clients (prompt "elicit"/"both"); False for a detached
    #: browser-only flow, whose callers are only told to finish in the browser.
    elicit: bool = True
    elicitation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    #: sessions that were handed this elicitation — notified on completion.
    notified: weakref.WeakSet[ServerSession] = field(default_factory=weakref.WeakSet)
    #: loopback callback server (no public_url) — cancelled once settled.
    callback_task: asyncio.Task[None] | None = None

    def expired(self) -> bool:
        return time.monotonic() >= self.expires_at

    def message(self) -> str:
        return f"Authorize the '{self.server}' MCP server to continue."


class AuthorizationPending(OAuthFlowError):
    """Raised out of the OAuth flow when the user must authorize out-of-band.

    An ``OAuthFlowError`` so the connection layer classifies it as an auth
    failure (no reconnect storm); :func:`complete` clears that state.
    """

    def __init__(self, pending: PendingAuthorization) -> None:
        super().__init__(f"Authorization required for '{pending.server}'")
        self.pending = pending


_pending: dict[str, PendingAuthorization] = {}
_on_authorized: Callable[[str], Awaitable[None]] | None = None


def set_on_authorized(hook: Callable[[str], Awaitable[None]] | None) -> None:
    """Register what runs after a server's tokens are stored (re-arm + reconnect)."""
    global _on_authorized
    _on_authorized = hook


def pending_for(server: str) -> PendingAuthorization | None:
    """The live pending authorization for *server*, dropping an expired one."""
    pending = _pending.get(server)
    if pending is not None and pending.expired():
        _discard(pending)
        return None
    return pending


def publish(pending: PendingAuthorization) -> None:
    """Record *pending* for its server, replacing (and settling) any older one."""
    previous = _pending.get(pending.server)
    if previous is not None and previous is not pending:
        _discard(previous)
    _pending[pending.server] = pending


def _discard(pending: PendingAuthorization) -> None:
    if _pending.get(pending.server) is pending:
        del _pending[pending.server]
    task = pending.callback_task
    if task is not None and not task.done() and task is not _current_task():
        task.cancel()


def _current_task() -> asyncio.Task[object] | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


class CallbackRejected(Exception):
    """The callback does not match the server's pending authorization."""


async def complete(server: str, code: str | None, state: str | None, error: str | None) -> None:
    """Finish *server*'s pending authorization from its OAuth redirect.

    Raises :class:`CallbackRejected` for a redirect that isn't the pending one
    (unknown server, no pending flow, state mismatch) and re-raises an exchange
    failure; either way the user sees the reason on the callback page.
    """
    pending = pending_for(server)
    if pending is None:
        raise CallbackRejected(f"no authorization is pending for '{server}'")
    if state is None or not secrets.compare_digest(state, pending.state):
        raise CallbackRejected("state mismatch — this link is stale; retry the tool call")
    _discard(pending)
    if error:
        raise CallbackRejected(f"authorization was not granted: {error}")
    if not code:
        raise CallbackRejected("the redirect carried no authorization code")
    await pending.exchange(code)
    logger.info("OAuth authorization completed for '%s'", server)
    if _on_authorized is not None:
        await _on_authorized(server)
    for session in list(pending.notified):
        try:
            await session.send_elicit_complete(pending.elicitation_id)
        except Exception as exc:  # noqa: BLE001 — a closed session must not fail the others
            logger.debug("elicitation/complete to a session failed: %s", exc)


def reset() -> None:
    """Drop every pending authorization (tests; combiner shutdown)."""
    for pending in list(_pending.values()):
        _discard(pending)


# ── presenting the URL to a caller ─────────────────────────────────────


def supports_url_elicitation(session: ServerSession | None) -> bool:
    """True when the client declared URL-mode elicitation in ``initialize``."""
    params = session.client_params if session is not None else None
    if params is None:
        return False
    elicitation = params.capabilities.elicitation
    return elicitation is not None and elicitation.url is not None


def prompt_error(pending: PendingAuthorization, session: ServerSession | None) -> Exception:
    """The error a tool call routed to *pending*'s server fails with.

    A client that supports URL elicitation gets ``-32042`` with the URL (shown
    to the user, not the model). Otherwise the URL goes in the tool error text —
    the only channel such a client has to the user. A browser-only flow never
    hands the URL out.
    """
    if not pending.elicit:
        return ToolError(
            f"Server '{pending.server}' is waiting for authorization in a browser on "
            "the combiner host; retry once it is complete."
        )
    if session is not None and supports_url_elicitation(session):
        pending.notified.add(session)
        return UrlElicitationRequiredError(
            [
                ElicitRequestURLParams(
                    mode="url",
                    message=pending.message(),
                    url=pending.url,
                    elicitationId=pending.elicitation_id,
                )
            ],
            message=f"Authorization required for '{pending.server}'",
        )
    return ToolError(
        f"Server '{pending.server}' needs authorization. Open this link to authorize, "
        f"then retry: {pending.url}"
    )


# ── opening a browser on the combiner host ─────────────────────────────


def has_local_browser() -> bool:
    """Whether opening a browser here can reach a user.

    False in containers and on Linux without a display — there the URL is only
    elicited/logged. Elsewhere defers to :mod:`webbrowser` finding a browser.
    """
    if Path("/.dockerenv").exists() or os.environ.get("container"):
        return False
    if sys.platform.startswith("linux") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        return False
    try:
        webbrowser.get()
    except webbrowser.Error:
        return False
    return True


def open_browser(url: str) -> bool:
    """Open *url* in a local browser when there is one; True if opened."""
    if not has_local_browser():
        logger.info("No local browser — not opening the authorization URL here")
        return False
    return webbrowser.open(url)
