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
from mcp.server.connection import Connection
from mcp.shared.exceptions import UrlElicitationRequiredError
from mcp.types import ElicitRequestURLParams

logger = logging.getLogger("mcp-combiner")

#: Exchanges an authorization code (with the redirect's RFC 9207 ``iss``, when
#: the server sent one) for tokens and stores them.
#: Returns the signed-in account when the provider says (an ``id_token``
#: email), else ``None``.
CodeExchange = Callable[[str, str | None], Awaitable[str | None]]


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
    #: client connections that were handed this elicitation — notified on completion.
    notified: weakref.WeakSet[Connection] = field(default_factory=weakref.WeakSet)
    #: loopback callback server (no public_url) — cancelled once settled.
    callback_task: asyncio.Task[None] | None = None
    #: the account to sign in with (auth.oauth.login_hint), when configured.
    login_hint: str | None = None

    def expired(self) -> bool:
        return time.monotonic() >= self.expires_at

    def message(self) -> str:
        account = f" — sign in as {self.login_hint}" if self.login_hint else ""
        return f"Authorize the '{self.server}' MCP server{account} to continue."


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


def pending_by_elicitation(elicitation_id: str) -> PendingAuthorization | None:
    """The live pending authorization with *elicitation_id* (the short link's key)."""
    for pending in list(_pending.values()):
        if secrets.compare_digest(pending.elicitation_id, elicitation_id):
            return pending_for(pending.server)
    return None


def short_link(pending: PendingAuthorization) -> str:
    """A short URL on the combiner that redirects to *pending*'s authorization URL.

    The real URL runs to ~400 characters (PKCE challenge, state, scopes) and
    wraps across lines, which stops terminals and many chat UIs recognising it
    as one link. The short one fits on a line. Keyed by the unguessable
    elicitation id, so it reveals no more than the URL it stands for. Served
    from ``oauth.public_url`` when set, else the combiner's loopback address;
    falls back to the full URL when neither is known.
    """
    from mcp_combiner.runtime import RUNTIME

    config = RUNTIME.config
    base = (config.oauth.public_url if config is not None else None) or RUNTIME.local_origin
    if base is None:
        return pending.url
    return f"{base}/oauth/authorize/{pending.elicitation_id}"


def _elicitation_url(pending: PendingAuthorization) -> str:
    from mcp_combiner.runtime import RUNTIME

    config = RUNTIME.config
    return short_link(pending) if config is not None and config.oauth.public_url else pending.url


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


@dataclass(frozen=True)
class Completed:
    """What a finished authorization can tell the user."""

    #: the account the provider says signed in, when it says (id_token email).
    account: str | None
    #: the configured auth.oauth.login_hint, if any.
    login_hint: str | None


async def complete(
    server: str,
    code: str | None,
    state: str | None,
    error: str | None,
    *,
    iss: str | None = None,
) -> Completed:
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
    account = await pending.exchange(code, iss)
    logger.info("OAuth authorization completed for '%s'", server)
    if _on_authorized is not None:
        await _on_authorized(server)
    for connection in list(pending.notified):
        # Best-effort by contract: Connection.notify drops (never raises) when
        # the client's standalone stream is gone.
        await connection.notify(
            "notifications/elicitation/complete", {"elicitationId": pending.elicitation_id}
        )
    return Completed(account=account, login_hint=pending.login_hint)


def reset() -> None:
    """Drop every pending authorization (tests; combiner shutdown)."""
    for pending in list(_pending.values()):
        _discard(pending)


# ── presenting the URL to a caller ─────────────────────────────────────


def supports_url_elicitation(connection: Connection) -> bool:
    """True when the client declared URL-mode elicitation."""
    capabilities = connection.client_capabilities
    elicitation = capabilities.elicitation if capabilities is not None else None
    return elicitation is not None and elicitation.url is not None


def prompt_error(pending: PendingAuthorization, connection: Connection | None) -> Exception:
    """The error a tool call routed to *pending*'s server fails with.

    A client that supports URL elicitation gets ``-32042`` with the URL (shown
    to the user, not the model). Otherwise the URL goes in the tool error text —
    the only channel such a client has to the user. A browser-only flow never
    hands the URL out. With ``oauth.public_url`` the URL is the combiner's short
    link, whose page (authpages) names the server before sending the user on to
    the provider; without it, the provider URL itself (the short link would be a
    loopback address no other device can open).
    """
    if not pending.elicit:
        return ToolError(
            f"Server '{pending.server}' is waiting for authorization in a browser on "
            "the combiner host; retry once it is complete."
        )
    if connection is not None and supports_url_elicitation(connection):
        pending.notified.add(connection)
        return UrlElicitationRequiredError(
            [
                ElicitRequestURLParams(
                    mode="url",
                    message=pending.message(),
                    url=_elicitation_url(pending),
                    elicitation_id=pending.elicitation_id,
                )
            ],
            message=f"Authorization required for '{pending.server}'",
        )
    # The short link, bare: a markdown link renders without being clickable
    # in some hosts, and the full URL is too long to survive wrapping.
    return ToolError(
        f"Server '{pending.server}' needs authorization"
        + (f" (sign in as {pending.login_hint})" if pending.login_hint else "")
        + f". Open {short_link(pending)} to authorize, then retry."
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
