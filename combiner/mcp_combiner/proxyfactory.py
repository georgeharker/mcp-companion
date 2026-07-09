"""Upstream proxy construction.

How a configured server becomes a mounted FastMCP provider — moved verbatim
from server.py:

- ``_create_server_proxy``: persistent-connection factory (HTTP/SSE via the
  ConnectionManager), auth-injected Client, or the config-dict path (stdio).
- ``_create_isolated_proxy``: per-chat upstream sessions for ``isolate: true``
  servers (StatefulProxyClient), incl. the OAuth-primer sharing/gating.
- ``_effective_isolate`` / ``_needs_oauth``: the tri-state isolate resolution
  and auth-mode probe.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastmcp import Client, FastMCP
from fastmcp.server import create_proxy
from fastmcp.server.providers.proxy import FastMCPProxy

from mcp_combiner.auth import build_auth
from mcp_combiner.config import CombinerConfig, ServerConfig
from mcp_combiner.connections import (
    AuthenticationError,
    ConnectionManager,
    build_http_transport,
)
from mcp_combiner.notifications import resource_notify_handler
from mcp_combiner.runtime import RUNTIME

logger = logging.getLogger("mcp-combiner")


def _effective_isolate(srv: ServerConfig) -> bool:
    """Resolve the tri-state ``isolate`` into the actual per-chat-session decision.

    ``isolate`` is ``None`` (absent → off), ``True`` (forced on) or ``False``
    (forced off). Per-chat isolation only applies to HTTP/SSE servers; stdio is
    never isolated (an explicit ``true`` there can't be honoured — it would need
    a subprocess per chat — so the caller logs a warning).
    """
    if not srv.isolate:
        return False
    if not ConnectionManager.is_http_server(srv):
        return False  # stdio: cannot isolate (caller warns)
    return True


def _create_server_proxy(config: CombinerConfig, name: str, srv: ServerConfig) -> FastMCP:
    """Create a proxy for a single upstream MCP server.

    When a persistent connection is available (HTTP/SSE servers), the proxy
    uses the connection manager's factory which returns the *already-connected*
    client — avoiding a connect/disconnect cycle per tool call.

    When the server has auth configured but no persistent connection, we
    create a ``Client`` with ``auth=`` set so the proxy's upstream HTTP
    requests carry the right credentials.

    For servers without auth and without a persistent connection we fall
    back to the simpler dict-based ``create_proxy(config_dict)`` path.

    Per-chat isolation (``isolate: true``, HTTP/SSE only): instead of one shared
    upstream session, a ``StatefulProxyClient`` opens a distinct upstream session
    per downstream chat (one server instance, shared transport), so a stateful
    server is handed a unique ``Mcp-Session-Id`` per chat. See ``_create_isolated_proxy``.
    """
    if _effective_isolate(srv):
        return _create_isolated_proxy(config, name, srv)

    # Prefer persistent connection if available
    if RUNTIME.conn_manager and RUNTIME.conn_manager.has_connection(name):
        factory = RUNTIME.conn_manager.get_client_factory(name)

        return FastMCPProxy(client_factory=factory, name=name)

    auth: httpx.Auth | None = build_auth(
        name,
        auth_config=srv.auth,
        server_url=srv.url,
        token_dir=config.oauth.token_dir_path,
        cache_tokens=config.oauth.cache_tokens,
    )

    if auth is not None and srv.url:
        # Auth requires a Client so we can inject httpx.Auth into the transport.
        client = Client(
            build_http_transport(srv),
            auth=auth,
            message_handler=resource_notify_handler(name),
        )
        return create_proxy(client, name=name)

    # No auth — use the standard config-dict path (preserves headers)
    proxy_config = config.to_fastmcp_config(name)
    return create_proxy(proxy_config.model_dump(exclude_none=True), name=name)


def _create_isolated_proxy(config: CombinerConfig, name: str, srv: ServerConfig) -> FastMCP:
    """Build a per-chat-isolated proxy for an HTTP/SSE server (``isolate: true``).

    Uses FastMCP's ``StatefulProxyClient``: a single base client (one transport,
    one server instance) whose ``new_stateful`` factory opens — and caches — a
    *separate upstream session per downstream chat*. The upstream server thus
    assigns a distinct, stable ``Mcp-Session-Id`` per chat, so a stateful server
    (e.g. svg-mcp's current document) partitions its state per chat with no
    clash between concurrent chats. The per-chat session is force-disconnected
    when the downstream session ends; an abandoned one is just an idle upstream
    HTTP session the server can expire — never a leaked process.

    OAuth servers (``isolate: true`` + ``auth: oauth``): the per-chat sessions
    share the auth object owned by the ``ConnectionManager`` "primer" connection
    (see ``_lifespan``), so the OAuth flow runs *once* on the primer (one browser
    window), the primer keeps the token refreshed, and per-chat sessions reuse it
    — distinct ``Mcp-Session-Id`` per chat, single credential. The per-chat
    factory gates on the primer's readiness so concurrent first-use chats never
    race into parallel auth flows, and surfaces the primer's auth failure as a
    retryable ``AuthenticationError``.
    """
    from fastmcp.server.providers.proxy import StatefulProxyClient

    transport = build_http_transport(srv)

    # OAuth-isolated: borrow the primer connection's shared, refreshed auth and
    # gate per-chat session creation on the primer's eager auth completing.
    if (
        _needs_oauth(srv)
        and RUNTIME.conn_manager is not None
        and RUNTIME.conn_manager.has_connection(name)
    ):
        mgr = RUNTIME.conn_manager
        shared_auth = mgr.get_auth(name)
        oauth_stateful: StatefulProxyClient[Any] = (
            StatefulProxyClient(
                transport, auth=shared_auth, message_handler=resource_notify_handler(name)
            )
            if shared_auth is not None
            else StatefulProxyClient(transport, message_handler=resource_notify_handler(name))
        )

        async def _gated_factory() -> Any:
            if mgr.is_auth_failed(name):
                raise AuthenticationError(
                    f"Server '{name}' is disabled due to an authentication error: "
                    f"{mgr.auth_error(name)}. Use combiner__enable_server to retry."
                )
            # Wait for the primer's first connect (and thus its OAuth flow) to
            # finish before opening this chat's session, so the token already
            # exists and concurrent first-use chats don't trigger parallel flows.
            await mgr.wait_ready(name)
            if mgr.is_auth_failed(name):
                raise AuthenticationError(
                    f"Server '{name}' is disabled due to an authentication error: "
                    f"{mgr.auth_error(name)}. Use combiner__enable_server to retry."
                )
            return oauth_stateful.new_stateful()

        logger.info(
            "Server '%s': per-chat session isolation enabled "
            "(isolate=true, OAuth shared via primer connection)",
            name,
        )
        return FastMCPProxy(client_factory=_gated_factory, name=name)

    # Non-OAuth (or static-header/bearer auth): self-contained. The auth, if any,
    # is held on the shared base client so per-chat sessions reuse it.
    auth: httpx.Auth | None = build_auth(
        name,
        auth_config=srv.auth,
        server_url=srv.url,
        token_dir=config.oauth.token_dir_path,
        cache_tokens=config.oauth.cache_tokens,
    )
    stateful: StatefulProxyClient[Any] = (
        StatefulProxyClient(
            transport, auth=auth, message_handler=resource_notify_handler(name)
        )
        if auth is not None
        else StatefulProxyClient(transport, message_handler=resource_notify_handler(name))
    )
    logger.info("Server '%s': per-chat session isolation enabled (isolate=true)", name)
    return FastMCPProxy(client_factory=stateful.new_stateful, name=name)


def _needs_oauth(srv: ServerConfig) -> bool:
    """Check if a server requires OAuth authentication."""
    if srv.auth == "oauth":
        return True
    if isinstance(srv.auth, dict) and "oauth" in srv.auth:
        return True
    return False
