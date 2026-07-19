"""End-to-end OAuth tests: the combiner's REAL _RefreshTokenOAuth against a
mock OAuth authorization server, over real HTTP loopback, in-process.

Unlike ``test_auth.py`` (which mocks ``httpx`` wholesale and never sees a wire
round-trip), these tests serve the mock's OAuth endpoints via uvicorn on a free
port and connect the combiner's authed ``Client`` to it.  They verify that the
combiner *gives* the right tokens to the upstream and *gets back* and stores the
right ones — the thing you actually rely on when the combiner manages OAuth:

- **Consent (perms) leg** — no cached tokens: discovery → dynamic client
  registration → authorize (PKCE) → code→token exchange, and the issued access
  token is attached as Bearer to the MCP call.
- **Refresh** — the correct ``refresh_token`` is sent, a rotated one is stored
  and persisted, and (Google-style) a refresh_token omitted from the response is
  preserved.
- **Bearer + refresh-on-expiry** through a live ``Client``.
- **Persistence across restart** — a fresh OAuth instance reuses cached
  tokens/registration and refreshes silently, with no second consent.

These stub only the browser-open + callback-server (the sole part unrunnable in
CI); the real token exchange runs against the mock served by uvicorn on a
loopback port.  Binding that real loopback socket is what the **nix build
sandbox forbids** — so despite having no subprocess or browser, they carry the
``e2e`` marker and run in ci.yml's e2e tier on a real runner, not in the
hermetic ``not e2e`` tier the nix ``flake check`` runs (see flake.nix).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import uvicorn
from conftest import free_port, poll_until
from fastmcp import Client
from fastmcp.client.transports.http import StreamableHttpTransport
from mcp.shared.auth import OAuthToken

from mcp_combiner.auth import _RefreshTokenOAuth, build_auth
from mcp_combiner.mockserver import (
    MockOAuthConfig,
    MockOAuthProvider,
    build_server,
)

# These tests bind a real loopback socket (uvicorn), which the nix build sandbox
# forbids — so they run in the e2e tier (ci.yml, real runner), not `flake check`.
pytestmark = pytest.mark.e2e


class _NoSignalServer(uvicorn.Server):
    """A uvicorn Server that never touches process-global signal handlers.

    Running many uvicorn instances in one pytest process is not what uvicorn's
    ``capture_signals`` was built for: each server swaps the process SIGINT/
    SIGTERM handlers for its own bound ``handle_exit`` and restores them only
    on a clean, LIFO unwind — overlapping servers scramble them. Worse,
    sse-starlette's shutdown watcher *introspects* ``signal.getsignal(SIGTERM)
    .__self__`` to find "the" uvicorn server and mirrors that server's
    ``should_exit`` into the process-global, never-reset
    ``AppStatus.should_exit`` — so one test's teardown (``should_exit = True``
    observed by a still-polling watcher) permanently poisons every later SSE
    response in the process: headers go out, the body drains instantly, and
    the MCP client hangs forever awaiting an InitializeResult that never
    arrives. Tests drive shutdown via ``should_exit``/cancellation, so signal
    capture is pure liability here.
    """

    @contextlib.contextmanager
    def capture_signals(self):  # noqa: ANN201 - matches uvicorn's signature
        yield


@pytest.fixture(autouse=True)
def _reset_sse_appstatus():
    """Reset sse-starlette's process-global shutdown latch around every test.

    ``AppStatus.should_exit`` is module-global and never reset once True (see
    _NoSignalServer). Belt-and-braces alongside _NoSignalServer: even if some
    other path sets it, no test inherits a poisoned latch.
    """
    from sse_starlette.sse import AppStatus

    AppStatus.should_exit = False
    yield
    AppStatus.should_exit = False


# ── in-process HTTP serving of the mock OAuth server ───────────────


@dataclass
class ServedOAuthMock:
    provider: MockOAuthProvider
    base_url: str
    port: int

    @property
    def mcp_url(self) -> str:
        return f"{self.base_url}/mcp"


@pytest.fixture
async def oauth_mock() -> AsyncIterator[Callable[..., object]]:
    """Yield a factory that boots a bearer-guarded mock OAuth MCP server.

    Each call serves a fresh mock on its own port via uvicorn in a background
    task; all are torn down when the fixture exits.
    """
    running: list[tuple[uvicorn.Server, asyncio.Task[None]]] = []

    async def make(config: MockOAuthConfig | None = None) -> ServedOAuthMock:
        port = free_port()
        issuer = f"http://127.0.0.1:{port}"
        provider = MockOAuthProvider(resource_url=f"{issuer}/mcp", issuer_url=issuer, config=config)
        mcp_srv, _state = build_server("mock", "http", oauth=provider)
        app = provider.guard(mcp_srv.http_app())
        server = _NoSignalServer(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        )
        task = asyncio.create_task(server.serve())
        running.append((server, task))

        async def _healthy() -> bool:
            async with httpx.AsyncClient() as c:
                r = await c.get(f"{issuer}/health", timeout=1.0)
                return r.status_code == 200

        await poll_until(_healthy, timeout=15.0, desc=f"mock oauth server :{port}")
        return ServedOAuthMock(provider=provider, base_url=issuer, port=port)

    yield make

    for server, task in running:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, timeout=5.0)
        if not task.done():
            # A server that ignored should_exit must not outlive its test —
            # a leaked serve task is exactly the cross-test poison this file
            # was suffering from (see _NoSignalServer).
            task.cancel()
            with contextlib.suppress(BaseException):
                await task


@pytest.fixture
def headless_consent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the browser-open + uvicorn callback server with a direct fetch.

    The only part of the authorization_code flow that can't run in CI is the
    browser redirect + local callback server.  We stub exactly that: capture the
    authorization URL, GET it against the mock (which 302s back with the code),
    and hand the code+state to the SDK — so the real code→token exchange still
    runs against the mock.
    """

    async def fake_redirect(self: _RefreshTokenOAuth, authorization_url: str) -> None:
        self._test_authorize_url = authorization_url

    async def fake_callback(self: _RefreshTokenOAuth) -> tuple[str, str | None]:
        url = self._test_authorize_url
        async with httpx.AsyncClient() as c:
            resp = await c.get(url, follow_redirects=False)
        assert resp.status_code == 302, f"authorize did not redirect: {resp.text}"
        query = parse_qs(urlparse(resp.headers["location"]).query)
        state = query["state"][0] if "state" in query else None
        return query["code"][0], state

    monkeypatch.setattr(_RefreshTokenOAuth, "redirect_handler", fake_redirect)
    monkeypatch.setattr(_RefreshTokenOAuth, "callback_handler", fake_callback)


def _oauth_for(mock: ServedOAuthMock, token_dir: Path, **kw: object) -> _RefreshTokenOAuth:
    auth = build_auth(
        "mock", auth_config="oauth", server_url=mock.mcp_url, token_dir=token_dir, **kw
    )
    assert isinstance(auth, _RefreshTokenOAuth)
    return auth


async def _call_echo(mock: ServedOAuthMock, oauth: _RefreshTokenOAuth, msg: str) -> str:
    transport = StreamableHttpTransport(url=mock.mcp_url)
    async with Client(transport, auth=oauth) as client:
        result = await client.call_tool("echo", {"message": msg})
    return str(result.content[0].text)


# ── the consent (perms) leg ────────────────────────────────────────


class TestConsentFlow:
    """First connection with no cached tokens runs the full authorization_code
    flow and attaches the issued access token to the MCP call."""

    async def test_full_flow_issues_and_attaches_tokens(
        self, oauth_mock: Callable[..., object], headless_consent: None, tmp_path: Path
    ) -> None:
        mock: ServedOAuthMock = await oauth_mock()  # type: ignore[misc]
        oauth = _oauth_for(mock, tmp_path)

        assert await _call_echo(mock, oauth, "hello") == "Echo: hello"

        audit = mock.provider.audit
        # Discovery-driven dynamic client registration happened exactly once.
        assert len(audit.registrations) == 1
        # Authorization request carried PKCE + response_type=code.
        assert len(audit.authorize_requests) == 1
        authz = audit.authorize_requests[0]
        assert authz["response_type"] == "code"
        assert authz["code_challenge_method"] == "S256"
        assert authz["client_id"] == audit.registrations[0]["client_id"]

        # The code was exchanged for tokens (PKCE verified server-side).
        code_grants = audit.token_requests_of("authorization_code")
        assert len(code_grants) == 1
        assert code_grants[0].client_id == authz["client_id"]
        assert audit.pkce_failures == 0

        # Exactly one (access, refresh) pair was issued...
        assert len(audit.issued) == 1
        issued = audit.issued[0]
        assert issued.refresh_token is not None
        # ...and the combiner GAVE that access token back on the MCP call.
        assert issued.access_token in audit.authenticated_mcp_calls()
        # ...and stored both tokens it GOT.
        assert oauth.context.current_tokens is not None
        assert oauth.context.current_tokens.access_token == issued.access_token
        assert oauth.context.current_tokens.refresh_token == issued.refresh_token

    async def test_unauthenticated_call_is_rejected_then_recovers(
        self, oauth_mock: Callable[..., object], headless_consent: None, tmp_path: Path
    ) -> None:
        """The mock 401s the first (tokenless) MCP request — that 401 is what
        drives the whole flow — and the retried call with a bearer succeeds."""
        mock: ServedOAuthMock = await oauth_mock()  # type: ignore[misc]
        oauth = _oauth_for(mock, tmp_path)

        assert await _call_echo(mock, oauth, "x") == "Echo: x"

        # The guard saw at least one tokenless probe (None) before a real token.
        assert None in mock.provider.audit.bearer_seen
        assert mock.provider.audit.authenticated_mcp_calls()


# ── refresh round-trips ─────────────────────────────────────────────


class TestRefresh:
    """The combiner sends the correct refresh_token and stores what it gets
    back — driven through the REAL _proactive_refresh over real HTTP."""

    def _seed_refreshable(
        self, oauth: _RefreshTokenOAuth, mock: ServedOAuthMock, refresh_token: str
    ) -> None:
        """Wire the OAuth context as if a prior flow left an expired access token
        plus a valid refresh token, and point it at the mock's token endpoint."""
        from unittest.mock import MagicMock

        mock.provider.seed_refresh_token(refresh_token)
        ctx = oauth.context
        ctx.current_tokens = OAuthToken(
            access_token="stale-access",
            token_type="Bearer",
            refresh_token=refresh_token,
            expires_in=-1,
        )
        ctx.token_expiry_time = time.time() - 100
        meta = MagicMock()
        meta.token_endpoint = f"{mock.base_url}/oauth/token"
        ctx.oauth_metadata = meta
        ci = MagicMock()
        ci.client_id = "client-seed"
        ctx.client_info = ci

    async def test_refresh_sends_right_token_and_stores_rotated(
        self, oauth_mock: Callable[..., object], tmp_path: Path
    ) -> None:
        from mcp_combiner.auth import _RefreshOutcome

        mock: ServedOAuthMock = await oauth_mock()  # type: ignore[misc]
        oauth = _oauth_for(mock, tmp_path)
        self._seed_refreshable(oauth, mock, "rt-seed")

        outcome = await oauth._proactive_refresh()
        assert outcome == _RefreshOutcome.SUCCESS

        # The combiner GAVE the exact refresh_token we held.
        refreshes = mock.provider.audit.token_requests_of("refresh_token")
        assert len(refreshes) == 1
        assert refreshes[0].refresh_token == "rt-seed"
        assert refreshes[0].client_id == "client-seed"

        # It GOT BACK a fresh, different access token and a rotated refresh token,
        # and stored both.
        issued = mock.provider.audit.issued[-1]
        assert issued.access_token != "stale-access"
        assert oauth.context.current_tokens is not None
        assert oauth.context.current_tokens.access_token == issued.access_token
        assert oauth.context.current_tokens.refresh_token == issued.refresh_token
        assert issued.refresh_token != "rt-seed"  # rotated

    async def test_refresh_token_preserved_when_provider_omits_it(
        self, oauth_mock: Callable[..., object], tmp_path: Path
    ) -> None:
        """Google-style: the refresh response omits refresh_token — the combiner
        must keep the original so it can keep refreshing silently."""
        from mcp_combiner.auth import _RefreshOutcome

        mock: ServedOAuthMock = await oauth_mock(  # type: ignore[misc]
            MockOAuthConfig(include_refresh_on_refresh=False)
        )
        oauth = _oauth_for(mock, tmp_path)
        self._seed_refreshable(oauth, mock, "rt-keep")

        outcome = await oauth._proactive_refresh()
        assert outcome == _RefreshOutcome.SUCCESS

        # Server issued no new refresh token...
        assert mock.provider.audit.issued[-1].refresh_token is None
        # ...but the combiner preserved the original.
        assert oauth.context.current_tokens is not None
        assert oauth.context.current_tokens.refresh_token == "rt-keep"

    async def test_refresh_with_unknown_token_is_auth_error(
        self, oauth_mock: Callable[..., object], tmp_path: Path
    ) -> None:
        """A refresh_token the server doesn't recognise → 4xx → AUTH_ERROR, and
        the combiner does NOT fabricate tokens."""
        from mcp_combiner.auth import _RefreshOutcome

        mock: ServedOAuthMock = await oauth_mock()  # type: ignore[misc]
        oauth = _oauth_for(mock, tmp_path)
        # Seed a refresh token but do NOT register it as valid on the server.
        self._seed_refreshable(oauth, mock, "rt-bogus")
        mock.provider.valid_refresh_tokens.discard("rt-bogus")

        outcome = await oauth._proactive_refresh()
        assert outcome == _RefreshOutcome.AUTH_ERROR
        # Original (stale) tokens left intact for the SDK's 401 path to handle.
        assert oauth.context.current_tokens is not None
        assert oauth.context.current_tokens.access_token == "stale-access"
        assert oauth.context.current_tokens.refresh_token == "rt-bogus"


# ── bearer attachment + refresh-on-expiry through a live Client ─────


class TestBearerLifecycle:
    async def test_expired_token_refreshed_and_new_bearer_attached(
        self, oauth_mock: Callable[..., object], headless_consent: None, tmp_path: Path
    ) -> None:
        mock: ServedOAuthMock = await oauth_mock()  # type: ignore[misc]
        oauth = _oauth_for(mock, tmp_path)

        # First call runs the consent flow and mints the initial pair.
        assert await _call_echo(mock, oauth, "one") == "Echo: one"
        first = mock.provider.audit.issued[0]
        assert oauth.context.current_tokens is not None
        held_refresh = oauth.context.current_tokens.refresh_token

        # Force the access token to look expired so the next call's pre-flight
        # refreshes it before touching /mcp.
        oauth.context.token_expiry_time = time.time() - 1

        assert await _call_echo(mock, oauth, "two") == "Echo: two"

        # A refresh happened, carrying the token we held...
        refreshes = mock.provider.audit.token_requests_of("refresh_token")
        assert refreshes and refreshes[-1].refresh_token == held_refresh
        # ...a second access token was issued...
        assert len(mock.provider.audit.issued) >= 2
        second = mock.provider.audit.issued[-1]
        assert second.access_token != first.access_token
        # ...and the fresh bearer is what reached the protected endpoint.
        assert second.access_token in mock.provider.audit.authenticated_mcp_calls()


# ── persistence across a combiner restart ──────────────────────────


class TestPersistence:
    """A fresh OAuth instance (simulating a combiner restart) built from the same
    on-disk token cache never re-runs the browser consent — it reuses the cached
    token when still valid, and refreshes silently when expired."""

    async def _expire_persisted_token(self, mock: ServedOAuthMock, token_dir: Path) -> None:
        """Rewrite FastMCP's on-disk absolute-expiry record into the past.

        FastMCP restores ``token_expiry_time`` from its ``{server_url}/token_expiry``
        key (collection ``mcp-oauth-token-expiry``); setting it to the past makes a
        subsequent instance deterministically see the cached access token as stale
        and refresh — no sleeping."""
        from mcp_combiner.auth import create_encrypted_store

        store = create_encrypted_store(token_dir / "mock")
        await store.put(
            key=f"{mock.mcp_url}/token_expiry",
            value={"expires_at": time.time() - 100},
            collection="mcp-oauth-token-expiry",
        )

    async def _drop_persisted_expiry(self, mock: ServedOAuthMock, token_dir: Path) -> None:
        """Delete the absolute-expiry key entirely — simulating a token cache
        written before FastMCP #2862: tokens present, but no persisted absolute
        expiry, so the loaded token's stale relative expires_in would otherwise
        make it look valid."""
        from mcp_combiner.auth import create_encrypted_store

        store = create_encrypted_store(token_dir / "mock")
        await store.delete(
            key=f"{mock.mcp_url}/token_expiry",
            collection="mcp-oauth-token-expiry",
        )

    async def test_valid_cached_token_reused_without_new_consent(
        self, oauth_mock: Callable[..., object], headless_consent: None, tmp_path: Path
    ) -> None:
        mock: ServedOAuthMock = await oauth_mock()  # type: ignore[misc]

        # Session 1: full consent flow, tokens persisted to tmp_path.
        oauth1 = _oauth_for(mock, tmp_path)
        assert await _call_echo(mock, oauth1, "first") == "Echo: first"
        first_access = mock.provider.audit.issued[0].access_token

        # Session 2: brand-new OAuth instance, same token_dir, token still valid.
        oauth2 = _oauth_for(mock, tmp_path)
        assert await _call_echo(mock, oauth2, "second") == "Echo: second"

        # No second registration, no second consent, no refresh needed — the
        # still-valid cached access token was reused verbatim.
        assert len(mock.provider.audit.registrations) == 1
        assert len(mock.provider.audit.authorize_requests) == 1
        assert not mock.provider.audit.token_requests_of("refresh_token")
        assert first_access in mock.provider.audit.authenticated_mcp_calls()

    async def test_expired_cached_token_refreshed_silently_on_restart(
        self, oauth_mock: Callable[..., object], headless_consent: None, tmp_path: Path
    ) -> None:
        mock: ServedOAuthMock = await oauth_mock()  # type: ignore[misc]

        # Session 1: full consent flow.
        oauth1 = _oauth_for(mock, tmp_path)
        assert await _call_echo(mock, oauth1, "first") == "Echo: first"
        held_refresh = mock.provider.audit.issued[0].refresh_token

        # Simulate a restart after the access token expired on disk.
        await self._expire_persisted_token(mock, tmp_path)

        # Session 2: fresh instance recovers by refreshing during init — no
        # browser consent, using the persisted refresh_token.
        oauth2 = _oauth_for(mock, tmp_path)
        assert await _call_echo(mock, oauth2, "second") == "Echo: second"

        assert len(mock.provider.audit.registrations) == 1
        assert len(mock.provider.audit.authorize_requests) == 1
        refreshes = mock.provider.audit.token_requests_of("refresh_token")
        assert refreshes and refreshes[-1].refresh_token == held_refresh
        # The freshly refreshed access token is what reached /mcp in session 2.
        refreshed_access = mock.provider.audit.issued[-1].access_token
        assert refreshed_access in mock.provider.audit.authenticated_mcp_calls()

    # Previously quarantined from CI for an ordering-dependent hang ("ASGI
    # callable returned without completing response"). Root-caused: not a
    # product bug — sse-starlette's process-global AppStatus.should_exit
    # latch, poisoned by an earlier test's uvicorn teardown via signal-handler
    # introspection. Fixed by _NoSignalServer + the _reset_sse_appstatus
    # autouse fixture, so it runs everywhere again.
    async def test_cache_without_persisted_expiry_bootstraps_one_refresh(
        self, oauth_mock: Callable[..., object], headless_consent: None, tmp_path: Path
    ) -> None:
        """Migration path: a legacy cache with no persisted absolute expiry
        triggers exactly ONE silent bootstrap refresh on the next start (the
        combiner refreshes when ``get_token_expiry()`` is None) — not a browser
        reauth, and not a loop."""
        mock: ServedOAuthMock = await oauth_mock()  # type: ignore[misc]

        # Session 1: consent flow persists tokens + FastMCP's expiry key.
        oauth1 = _oauth_for(mock, tmp_path)
        assert await _call_echo(mock, oauth1, "first") == "Echo: first"
        held_refresh = mock.provider.audit.issued[0].refresh_token
        assert not mock.provider.audit.token_requests_of("refresh_token")

        # Simulate a pre-#2862 cache: tokens on disk, absolute expiry missing.
        await self._drop_persisted_expiry(mock, tmp_path)

        # Session 2: fresh instance bootstraps one refresh — no re-consent, even
        # though the cached access token's clock-time hasn't actually expired.
        oauth2 = _oauth_for(mock, tmp_path)
        assert await _call_echo(mock, oauth2, "second") == "Echo: second"
        assert len(mock.provider.audit.registrations) == 1
        assert len(mock.provider.audit.authorize_requests) == 1
        refreshes = mock.provider.audit.token_requests_of("refresh_token")
        assert len(refreshes) == 1
        assert refreshes[0].refresh_token == held_refresh
        bootstrapped = mock.provider.audit.issued[-1].access_token
        assert bootstrapped in mock.provider.audit.authenticated_mcp_calls()

        # Session 3: the bootstrap refresh re-persisted a real absolute expiry, so
        # this start does NOT refresh again — proves it's one-time, not a loop.
        oauth3 = _oauth_for(mock, tmp_path)
        assert await _call_echo(mock, oauth3, "third") == "Echo: third"
        assert len(mock.provider.audit.token_requests_of("refresh_token")) == 1
