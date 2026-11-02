"""Out-of-band OAuth authorization (authprompt): the authorization URL reaches
the user through the MCP client (URL-mode elicitation, ``-32042``) instead of
— or as well as — a browser on the combiner host.

Unit tests cover config resolution, the provider's redirect URI and detached
mode, and the pending-authorization lifecycle. The e2e tests run the real flow
against the mock OAuth server: the loopback callback at the provider level, and
the full chain through a combiner subprocess with ``oauth.public_url`` — a
downstream client gets ``-32042``, "the user" follows the link, the combiner's
``/oauth/callback/<server>`` completes it, and the retried call succeeds.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import mcp.types as mt
import pytest
from conftest import (
    OAuthMockFactory,
    ProcFactory,
    ServedOAuthMock,
    free_port,
    poll_until,
    write_servers_config,
)
from fakes import content_text
from fastmcp import Client
from fastmcp.client.messages import MessageHandler
from fastmcp.client.transports.http import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from mcp import ClientSession
from mcp.server.session import ServerSession
from mcp.shared.context import RequestContext
from mcp.shared.exceptions import McpError, UrlElicitationRequiredError

from mcp_combiner import authprompt
from mcp_combiner.auth import _RefreshTokenOAuth, authorizes_out_of_band, build_auth
from mcp_combiner.config import CombinerConfig, OAuthConfig, resolve_auth_prompt


@pytest.fixture(autouse=True)
def _clean_authprompt() -> Iterator[None]:
    authprompt.reset()
    authprompt.set_on_authorized(None)
    yield
    authprompt.reset()
    authprompt.set_on_authorized(None)


class _Session(ServerSession):
    """A downstream session reduced to what authprompt reads and sends: the
    client's declared capabilities and ``elicitation/complete``."""

    def __init__(self, *, url_elicitation: bool) -> None:
        elicitation = (
            mt.ElicitationCapability(url=mt.UrlElicitationCapability()) if url_elicitation else None
        )
        self._client_params = mt.InitializeRequestParams(
            protocolVersion=mt.LATEST_PROTOCOL_VERSION,
            capabilities=mt.ClientCapabilities(elicitation=elicitation),
            clientInfo=mt.Implementation(name="test", version="0"),
        )
        self.completed: list[str] = []

    async def send_elicit_complete(
        self, elicitation_id: str, related_request_id: mt.RequestId | None = None
    ) -> None:
        self.completed.append(elicitation_id)


def _pending(
    server: str = "srv",
    *,
    exchange: authprompt.CodeExchange | None = None,
    elicit: bool = True,
    ttl: float = 60.0,
) -> authprompt.PendingAuthorization:
    async def _no_exchange(code: str) -> None:
        return None

    return authprompt.PendingAuthorization(
        server=server,
        url="https://auth.example/authorize?state=s1",
        state="s1",
        exchange=exchange or _no_exchange,
        expires_at=time.monotonic() + ttl,
        elicit=elicit,
    )


# ── config ─────────────────────────────────────────────────────────


class TestPromptConfig:
    def test_default_keeps_browser_without_public_url(self) -> None:
        assert resolve_auth_prompt("s", None, None, None) == "browser"

    def test_default_elicits_with_public_url(self) -> None:
        assert resolve_auth_prompt("s", None, None, "https://c.ts.net") == "both"

    def test_server_prompt_overrides_global(self) -> None:
        assert resolve_auth_prompt("s", "elicit", "browser", None) == "elicit"
        assert resolve_auth_prompt("s", None, "elicit", None) == "elicit"

    def test_invalid_prompt_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="oauth prompt must be one of"):
            resolve_auth_prompt("s", "popup", None, None)

    def test_global_block_parses_public_url_and_prompt(self) -> None:
        cfg = OAuthConfig.from_dict({"public_url": "https://c.ts.net/", "prompt": "elicit"})
        assert cfg.public_url == "https://c.ts.net"
        assert cfg.prompt == "elicit"
        with pytest.raises(ValueError):
            OAuthConfig.from_dict({"prompt": "sometimes"})

    def _load(self, tmp_path: Path, doc: dict[str, Any]) -> CombinerConfig:
        path = tmp_path / "servers.json"
        path.write_text(json.dumps(doc))
        return CombinerConfig.load(str(path))

    def test_elicit_without_public_url_warns_but_loads(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        server = {"url": "http://x/mcp", "auth": {"oauth": {"prompt": "elicit"}}}
        with caplog.at_level(logging.WARNING, logger="mcp-combiner"):
            cfg = self._load(tmp_path, {"servers": {"g": server}})
        assert "g" in cfg.servers
        assert "without oauth.public_url" in caplog.text

    def test_public_url_silences_the_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        doc = {
            "servers": {"g": {"url": "http://x/mcp", "auth": "oauth"}},
            "oauth": {"public_url": "https://c.ts.net"},
        }
        with caplog.at_level(logging.WARNING, logger="mcp-combiner"):
            self._load(tmp_path, doc)
        assert "public_url" not in caplog.text

    def test_invalid_server_prompt_fails_the_load(self, tmp_path: Path) -> None:
        server = {"url": "http://x/mcp", "auth": {"oauth": {"prompt": "nope"}}}
        with pytest.raises(ValueError, match="Server 'g'"):
            self._load(tmp_path, {"servers": {"g": server}})


# ── the provider ───────────────────────────────────────────────────


def _oauth(tmp_path: Path, **kw: Any) -> _RefreshTokenOAuth:
    auth = build_auth(
        "g", auth_config=kw.pop("auth_config", "oauth"), server_url="http://up/mcp", **kw
    )
    assert isinstance(auth, _RefreshTokenOAuth)
    return auth


class TestProvider:
    def test_browser_on_loopback_keeps_the_inline_flow(self, tmp_path: Path) -> None:
        oauth = _oauth(tmp_path, token_dir=tmp_path)
        uris = oauth.context.client_metadata.redirect_uris
        assert uris is not None
        assert str(uris[0]) == f"http://localhost:{oauth.redirect_port}/callback"
        assert not oauth.detached
        assert not authorizes_out_of_band(oauth)

    def test_public_url_redirects_to_the_combiner(self, tmp_path: Path) -> None:
        oauth = _oauth(tmp_path, token_dir=tmp_path, public_url="https://c.ts.net")
        uris = oauth.context.client_metadata.redirect_uris
        assert uris is not None
        assert str(uris[0]) == "https://c.ts.net/oauth/callback/g"
        assert oauth.prompt == "both"
        assert authorizes_out_of_band(oauth)

    def test_public_url_patches_static_client_registration(self, tmp_path: Path) -> None:
        oauth = _oauth(
            tmp_path,
            token_dir=tmp_path,
            public_url="https://c.ts.net",
            auth_config={"oauth": {"client_id": "web-client", "client_secret": "s"}},
        )
        static = oauth._static_client_info
        assert static is not None and static.redirect_uris is not None
        assert [str(u) for u in static.redirect_uris] == ["https://c.ts.net/oauth/callback/g"]

    def test_elicit_on_loopback_is_detached(self, tmp_path: Path) -> None:
        oauth = _oauth(tmp_path, token_dir=tmp_path, auth_config={"oauth": {"prompt": "elicit"}})
        assert oauth.detached
        assert oauth.public_url is None


# ── presenting a pending authorization ─────────────────────────────


class TestPromptError:
    def test_capable_client_gets_url_elicitation(self) -> None:
        pending = _pending()
        session = _Session(url_elicitation=True)
        err = authprompt.prompt_error(pending, session)
        assert isinstance(err, UrlElicitationRequiredError)
        assert err.error.code == mt.URL_ELICITATION_REQUIRED
        assert [e.url for e in err.elicitations] == [pending.url]
        assert err.elicitations[0].elicitationId == pending.elicitation_id
        assert session in pending.notified

    def test_other_clients_get_the_url_in_the_tool_error(self) -> None:
        pending = _pending()
        err = authprompt.prompt_error(pending, _Session(url_elicitation=False))
        assert isinstance(err, ToolError)
        assert pending.url in str(err)
        assert not pending.notified

    def test_browser_only_flow_never_hands_out_the_url(self) -> None:
        pending = _pending(elicit=False)
        err = authprompt.prompt_error(pending, _Session(url_elicitation=True))
        assert isinstance(err, ToolError)
        assert pending.url not in str(err)


class TestComplete:
    async def test_success_exchanges_rearms_and_notifies(self) -> None:
        exchanged: list[str] = []
        authorized: list[str] = []

        async def exchange(code: str) -> None:
            exchanged.append(code)

        async def on_authorized(server: str) -> None:
            authorized.append(server)

        authprompt.set_on_authorized(on_authorized)
        pending = _pending(exchange=exchange)
        session = _Session(url_elicitation=True)
        pending.notified.add(session)
        authprompt.publish(pending)

        await authprompt.complete("srv", "the-code", "s1", None)

        assert exchanged == ["the-code"]
        assert authorized == ["srv"]
        assert session.completed == [pending.elicitation_id]
        assert authprompt.pending_for("srv") is None

    @pytest.mark.parametrize(
        ("server", "code", "state", "error", "reason"),
        [
            ("other", "c", "s1", None, "no authorization is pending"),
            ("srv", "c", "wrong", None, "state mismatch"),
            ("srv", "c", None, None, "state mismatch"),
            ("srv", None, "s1", "access_denied", "not granted"),
            ("srv", None, "s1", None, "no authorization code"),
        ],
    )
    async def test_rejected_callbacks(
        self, server: str, code: str | None, state: str | None, error: str | None, reason: str
    ) -> None:
        exchanged: list[str] = []

        async def exchange(code: str) -> None:
            exchanged.append(code)

        authprompt.publish(_pending(exchange=exchange))
        with pytest.raises(authprompt.CallbackRejected, match=reason):
            await authprompt.complete(server, code, state, error)
        assert exchanged == []

    def test_expired_pending_is_dropped(self) -> None:
        authprompt.publish(_pending(ttl=-1.0))
        assert authprompt.pending_for("srv") is None

    def test_publish_replaces_an_older_pending(self) -> None:
        first, second = _pending(), _pending()
        authprompt.publish(first)
        authprompt.publish(second)
        assert authprompt.pending_for("srv") is second


# ── e2e: the real flow against the mock OAuth server ───────────────


async def _follow_as_user(url: str) -> httpx.Response:
    """What the user's browser does: open the link and follow the redirects
    (the mock authorization server auto-consents)."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as http:
        return await http.get(url)


@pytest.mark.e2e
class TestLoopbackDetachedFlow:
    async def test_elicit_without_public_url_completes_on_the_loopback(
        self, oauth_mock: OAuthMockFactory, tmp_path: Path
    ) -> None:
        mock: ServedOAuthMock = await oauth_mock()
        oauth = build_auth(
            "mock",
            auth_config={"oauth": {"prompt": "elicit"}},
            server_url=mock.mcp_url,
            token_dir=tmp_path,
        )
        assert isinstance(oauth, _RefreshTokenOAuth)

        # The connect fails fast — no waiting on the user — with the link published.
        with pytest.raises(Exception):
            async with Client(StreamableHttpTransport(url=mock.mcp_url), auth=oauth):
                pass
        pending = authprompt.pending_for("mock")
        assert pending is not None
        assert f"localhost%3A{oauth.redirect_port}" in pending.url

        page = await _follow_as_user(pending.url)
        assert page.status_code == 200

        async def _tokens() -> bool:
            return oauth.context.current_tokens is not None

        await poll_until(_tokens, timeout=15.0, desc="tokens stored after callback")
        assert authprompt.pending_for("mock") is None

        async with Client(StreamableHttpTransport(url=mock.mcp_url), auth=oauth) as client:
            result = await client.call_tool("echo", {"message": "hi"})
        assert content_text(result) == "Echo: hi"
        assert mock.provider.audit.pkce_failures == 0


class _Notifications(MessageHandler):
    def __init__(self) -> None:
        self.seen: list[mt.ServerNotification] = []

    async def on_notification(self, message: mt.ServerNotification) -> None:
        self.seen.append(message)

    def elicitation_completed(self) -> list[str]:
        return [
            n.root.params.elicitationId
            for n in self.seen
            if isinstance(n.root, mt.ElicitCompleteNotification)
        ]


async def _never_elicit(
    message: str,
    response_type: type[Any] | None,
    params: mt.ElicitRequestParams,
    context: RequestContext[ClientSession, Any],
) -> dict[str, Any]:
    raise AssertionError(f"unexpected elicitation/create: {message}")


@pytest.mark.e2e
class TestCombinerPublicUrlFlow:
    """The full chain through a combiner subprocess: startup auth goes
    out-of-band, the client is handed the link, the combiner's own
    /oauth/callback/<server> completes it, and the call then succeeds."""

    async def _start(
        self,
        procs: ProcFactory,
        mock: ServedOAuthMock,
        tmp_path: Path,
        env: dict[str, str] | None = None,
    ) -> str:
        port = free_port()
        config = write_servers_config(
            tmp_path / "servers.json",
            {"mock": {"url": mock.mcp_url, "transport": "http", "auth": "oauth"}},
            oauth={
                "public_url": f"http://127.0.0.1:{port}",
                "prompt": "elicit",
                "token_dir": str(tmp_path / "tokens"),
            },
        )
        combiner = await procs.start_combiner(
            config,
            port=port,
            # Kept for diagnosis: the subprocess has no other output.
            extra_args=["--log-file", str(tmp_path / "combiner.log")],
            env=env,
        )
        await combiner.wait_server_state("mock", ("auth_failed",))
        return combiner.mcp_url

    async def test_elicitation_capable_client(
        self, procs: ProcFactory, oauth_mock: OAuthMockFactory, tmp_path: Path
    ) -> None:
        mock = await oauth_mock()
        mcp_url = await self._start(procs, mock, tmp_path)
        notes = _Notifications()

        async with Client(
            mcp_url, elicitation_handler=_never_elicit, message_handler=notes
        ) as client:
            with pytest.raises(McpError) as caught:
                await client.call_tool("mock_echo", {"message": "hi"})
            error = caught.value.error
            assert error.code == mt.URL_ELICITATION_REQUIRED
            assert isinstance(error.data, dict)
            (elicitation,) = error.data["elicitations"]
            assert "%2Foauth%2Fcallback%2Fmock" in elicitation["url"]

            page = await _follow_as_user(elicitation["url"])
            assert page.status_code == 200
            assert "is authorized" in page.text

            async def _completed() -> list[str]:
                return notes.elicitation_completed()

            done = await poll_until(_completed, timeout=15.0, desc="elicitation/complete")
            assert done == [elicitation["elicitationId"]]

            async def _echo() -> str | None:
                try:
                    return content_text(await client.call_tool("mock_echo", {"message": "hi"}))
                except ToolError:
                    return None

            assert await poll_until(_echo, timeout=30.0, desc="call after auth") == "Echo: hi"

    async def test_client_without_url_elicitation_gets_the_link_as_text(
        self, procs: ProcFactory, oauth_mock: OAuthMockFactory, tmp_path: Path
    ) -> None:
        mock = await oauth_mock()
        mcp_url = await self._start(procs, mock, tmp_path)

        async with Client(mcp_url) as client:
            with pytest.raises(ToolError, match="needs authorization") as caught:
                await client.call_tool("mock_echo", {"message": "hi"})
        assert "/oauth/authorize?" in str(caught.value)

    async def test_bad_state_is_refused_by_the_callback(
        self, procs: ProcFactory, oauth_mock: OAuthMockFactory, tmp_path: Path
    ) -> None:
        mock = await oauth_mock()
        mcp_url = await self._start(procs, mock, tmp_path)
        base = mcp_url.removesuffix("/mcp")
        async with httpx.AsyncClient() as http:
            r = await http.get(f"{base}/oauth/callback/mock", params={"code": "x", "state": "y"})
        assert r.status_code == 400
        assert "state mismatch" in r.text

    async def test_expired_link_is_replaced_on_the_next_call(
        self, procs: ProcFactory, oauth_mock: OAuthMockFactory, tmp_path: Path
    ) -> None:
        """Once a link expires nothing retries on its own; the next call routed
        to the server re-runs the flow and is handed a fresh link."""
        mock = await oauth_mock()
        mcp_url = await self._start(procs, mock, tmp_path, env={"MCP_COMBINER_AUTH_LINK_TTL": "2"})

        async def _link(client: Client[Any]) -> tuple[str, str]:
            with pytest.raises(McpError) as caught:
                await client.call_tool("mock_echo", {"message": "hi"})
            assert caught.value.error.code == mt.URL_ELICITATION_REQUIRED
            assert isinstance(caught.value.error.data, dict)
            (elicitation,) = caught.value.error.data["elicitations"]
            return elicitation["url"], elicitation["elicitationId"]

        async with Client(mcp_url, elicitation_handler=_never_elicit) as client:
            first_url, first_id = await _link(client)
            await asyncio.sleep(2.5)
            second_url, second_id = await _link(client)
            assert (second_url, second_id) != (first_url, first_id)

            page = await _follow_as_user(second_url)
            assert page.status_code == 200, page.text

            async def _echo() -> str | None:
                try:
                    return content_text(await client.call_tool("mock_echo", {"message": "hi"}))
                except (ToolError, McpError):
                    return None

            assert await poll_until(_echo, timeout=30.0, desc="call after auth") == "Echo: hi"
