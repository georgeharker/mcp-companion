"""E2E chat-session independence: concurrent MCP clients with distinct chat
tokens against one combiner.

Covers both token transports (header ``X-MCP-Combiner-Session`` and URL path
``/mcp/<token>`` — the two TokenRewriteMiddleware paths), token→session
mapping, per-session filter isolation, pending filters, and per-chat upstream
session isolation for ``isolate: true`` servers (observed from the upstream
side via the mock's session tracking).

NOTE (QUESTIONS.md Q1): the token REST route and the fastmcp middleware
currently key sessions in two different namespaces (HTTP ``mcp-session-id``
vs ``Context.session_id``), so filters set via /sessions/token/<t>/filter are
recorded but never applied. Tests pinning the *intended* behavior of that
path are marked ``xfail(strict=True)`` — they flip loudly when the namespace
split is resolved. The meta-tool path works and is pinned as passing.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx
import pytest
from conftest import (
    CombinerHandle,
    ProcFactory,
    http_mock_entry,
    stdio_mock_entry,
    write_servers_config,
    write_tools_spec,
)
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

pytestmark = pytest.mark.e2e

xfail_session_namespace_split = pytest.mark.xfail(
    strict=True,
    reason="QUESTIONS.md Q1: token-route filters keyed by HTTP mcp-session-id, "
    "middleware filters by Context.session_id — namespaces diverge on fastmcp 3.4",
)

_SPEC = [
    {"name": "greet", "params": {"who": "string"}, "response_template": "Hello, {who}!"},
]


def _token() -> str:
    return str(uuid.uuid4())


def _header_client(combiner: CombinerHandle, token: str) -> Client:
    return Client(
        StreamableHttpTransport(combiner.mcp_url, headers={"X-MCP-Combiner-Session": token})
    )


def _url_client(combiner: CombinerHandle, token: str) -> Client:
    return Client(f"{combiner.mcp_url}/{token}")


async def _text(client: Client, tool: str, args: dict) -> str:
    result = await client.call_tool(tool, args)
    return result.content[0].text


async def _rest(combiner: CombinerHandle, method: str, path: str, body: dict | None = None) -> dict:
    async with httpx.AsyncClient() as http:
        r = await http.request(method, f"{combiner.base_url}{path}", json=body, timeout=5.0)
        r.raise_for_status()
        return r.json()


async def _start_stdio_combiner(procs: ProcFactory, tmp_path: Path) -> CombinerHandle:
    tools_path = write_tools_spec(tmp_path / "tools.json", _SPEC)
    cfg = write_servers_config(
        tmp_path / "servers.json",
        {"mockup": stdio_mock_entry("mockup", tools_path=tools_path)},
    )
    combiner = await procs.start_combiner(cfg)
    await combiner.wait_server_state("mockup", ("ready",))
    return combiner


class TestTokenSessionMapping:
    async def test_concurrent_clients_get_distinct_sessions(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        """Header and URL token paths both map, and three concurrent chats get
        three distinct combiner sessions."""
        combiner = await _start_stdio_combiner(procs, tmp_path)
        tok_a, tok_b, tok_c = _token(), _token(), _token()

        async with (
            _header_client(combiner, tok_a) as ca,
            _header_client(combiner, tok_b) as cb,
            _url_client(combiner, tok_c) as cc,
        ):
            for c in (ca, cb, cc):
                assert await _text(c, "mockup_greet", {"who": "x"}) == "Hello, x!"

            sids = set()
            for tok in (tok_a, tok_b, tok_c):
                out = await _rest(combiner, "GET", f"/sessions/token/{tok}")
                assert out["session_id"], f"token {tok} not mapped"
                sids.add(out["session_id"])
            assert len(sids) == 3

    async def test_prefixed_opaque_tokens_map_like_uuids(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        """Tokens are opaque, externally minted strings — the Claude plugin's
        cc-<session-id> and arbitrary user overrides must map exactly like
        nvim's bare UUIDs, on both the header and URL-path transports. (A
        UUID-only validation regex used to silently demote these to the
        tokenless path.)"""
        combiner = await _start_stdio_combiner(procs, tmp_path)
        tok_header = f"cc-{_token()}"
        tok_url = "my.custom_token-42"

        async with (
            _header_client(combiner, tok_header) as ch,
            _url_client(combiner, tok_url) as cu,
        ):
            assert await _text(ch, "mockup_greet", {"who": "h"}) == "Hello, h!"
            assert await _text(cu, "mockup_greet", {"who": "u"}) == "Hello, u!"

            for tok in (tok_header, tok_url):
                out = await _rest(combiner, "GET", f"/sessions/token/{tok}")
                assert out["session_id"], f"token {tok} not mapped"

    @xfail_session_namespace_split
    async def test_token_sessions_appear_in_sessions_listing(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        """The session ids reported for tokens should be joinable against the
        /sessions listing (currently: hex HTTP ids vs dashed Context ids)."""
        combiner = await _start_stdio_combiner(procs, tmp_path)
        tok = _token()
        async with _header_client(combiner, tok) as c:
            await _text(c, "mockup_greet", {"who": "x"})
            out = await _rest(combiner, "GET", f"/sessions/token/{tok}")
            sessions = await _rest(combiner, "GET", "/sessions")
            listed = {s["session_id"] for s in sessions["sessions"]}
            assert out["session_id"] in listed


class TestMetaToolFilterIsolation:
    """The combiner__session_* meta-tools key on Context.session_id end to end
    — this path works and pins session independence between concurrent chats."""

    async def test_disable_for_one_session_leaves_others_untouched(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        combiner = await _start_stdio_combiner(procs, tmp_path)
        tok_a, tok_b = _token(), _token()

        async with (
            _header_client(combiner, tok_a) as ca,
            _header_client(combiner, tok_b) as cb,
        ):
            assert await _text(ca, "mockup_greet", {"who": "a"}) == "Hello, a!"
            assert await _text(cb, "mockup_greet", {"who": "b"}) == "Hello, b!"

            out = json.loads(
                await _text(ca, "combiner__session_disable_server", {"server_name": "mockup"})
            )
            assert out["disabled_servers"] == ["mockup"]

            # A: tools hidden and calls blocked.
            names_a = {t.name for t in await ca.list_tools()}
            assert not any(n.startswith("mockup_") for n in names_a)
            with pytest.raises(Exception, match="disabled|unknown|not found"):
                await ca.call_tool("mockup_greet", {"who": "nope"})

            # B: fully unaffected.
            names_b = {t.name for t in await cb.list_tools()}
            assert "mockup_greet" in names_b
            assert await _text(cb, "mockup_greet", {"who": "still"}) == "Hello, still!"
            status_b = json.loads(await _text(cb, "combiner__session_status", {}))
            assert status_b["disabled_servers"] == []

            # Re-enable A and it recovers.
            await _text(ca, "combiner__session_enable_server", {"server_name": "mockup"})
            names_a2 = {t.name for t in await ca.list_tools()}
            assert "mockup_greet" in names_a2
            assert await _text(ca, "mockup_greet", {"who": "back"}) == "Hello, back!"

    async def test_sessions_do_not_share_filter_state(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        combiner = await _start_stdio_combiner(procs, tmp_path)
        tokens = [_token() for _ in range(3)]
        clients = [_header_client(combiner, t) for t in tokens]

        async with clients[0] as c0, clients[1] as c1, clients[2] as c2:
            for c in (c0, c1, c2):
                await _text(c, "mockup_greet", {"who": "x"})
            await _text(c1, "combiner__session_disable_server", {"server_name": "mockup"})

            visible = []
            for c in (c0, c1, c2):
                names = {t.name for t in await c.list_tools()}
                visible.append(any(n.startswith("mockup_") for n in names))
            assert visible == [True, False, True]


class TestControlChannelFilters:
    """The Lua plugin's real operating pattern: a TOKENLESS control session
    (the editor's singleton client on /mcp) manages filters for OTHER chats'
    sessions, naming the target by chat token (REST route) or by the
    ``chat_id`` meta-tool argument. Both naming paths currently miss the
    target session's actual filter key (Q1 / Q1b)."""

    @xfail_session_namespace_split
    async def test_control_session_filters_chat_via_token_route(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        combiner = await _start_stdio_combiner(procs, tmp_path)
        tok = _token()
        async with (
            Client(combiner.mcp_url) as control,  # tokenless control channel
            _header_client(combiner, tok) as chat,
        ):
            assert await _text(chat, "mockup_greet", {"who": "x"}) == "Hello, x!"
            # Control channel is a different session — its own tools unaffected.
            await _rest(combiner, "POST", f"/sessions/token/{tok}/filter", {"disable": "mockup"})

            chat_names = {t.name for t in await chat.list_tools()}
            assert not any(n.startswith("mockup_") for n in chat_names)
            control_names = {t.name for t in await control.list_tools()}
            assert "mockup_greet" in control_names

    @xfail_session_namespace_split
    async def test_control_session_filters_chat_via_meta_tool_chat_id(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        """chat_id names the target chat when the CALLER is not that chat.
        Currently the value is stored verbatim as the filter key, which never
        matches the chat session's Context.session_id (Q1b)."""
        combiner = await _start_stdio_combiner(procs, tmp_path)
        tok = _token()
        async with (
            Client(combiner.mcp_url) as control,
            _header_client(combiner, tok) as chat,
        ):
            assert await _text(chat, "mockup_greet", {"who": "x"}) == "Hello, x!"
            out = json.loads(
                await _text(
                    control,
                    "combiner__session_disable_server",
                    {"server_name": "mockup", "chat_id": tok},
                )
            )
            assert "mockup" in out["disabled_servers"]

            chat_names = {t.name for t in await chat.list_tools()}
            assert not any(n.startswith("mockup_") for n in chat_names)


class TestTokenRouteFilters:
    """The REST /sessions/token/<t>/filter route — the Lua plugin's primary
    filter path. Bookkeeping works; APPLICATION is currently broken (Q1)."""

    async def test_token_filter_bookkeeping(self, procs: ProcFactory, tmp_path: Path) -> None:
        combiner = await _start_stdio_combiner(procs, tmp_path)
        tok = _token()
        async with _header_client(combiner, tok) as c:
            await _text(c, "mockup_greet", {"who": "x"})
            out = await _rest(
                combiner, "POST", f"/sessions/token/{tok}/filter", {"disable": "mockup"}
            )
            assert out["disabled_servers"] == ["mockup"]
            status = await _rest(combiner, "GET", f"/sessions/token/{tok}/filter")
            assert status["disabled_servers"] == ["mockup"]
            out = await _rest(
                combiner, "POST", f"/sessions/token/{tok}/filter", {"enable": "mockup"}
            )
            assert out["disabled_servers"] == []

    @xfail_session_namespace_split
    async def test_token_filter_actually_filters(self, procs: ProcFactory, tmp_path: Path) -> None:
        combiner = await _start_stdio_combiner(procs, tmp_path)
        tok_a, tok_b = _token(), _token()
        async with (
            _header_client(combiner, tok_a) as ca,
            _header_client(combiner, tok_b) as cb,
        ):
            assert await _text(ca, "mockup_greet", {"who": "a"}) == "Hello, a!"
            assert await _text(cb, "mockup_greet", {"who": "b"}) == "Hello, b!"

            await _rest(combiner, "POST", f"/sessions/token/{tok_a}/filter", {"disable": "mockup"})

            names_a = {t.name for t in await ca.list_tools()}
            assert not any(n.startswith("mockup_") for n in names_a)
            names_b = {t.name for t in await cb.list_tools()}
            assert "mockup_greet" in names_b

    @xfail_session_namespace_split
    async def test_allowed_servers_inverts_and_applies(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        combiner = await _start_stdio_combiner(procs, tmp_path)
        tok = _token()
        async with _header_client(combiner, tok) as c:
            await _text(c, "mockup_greet", {"who": "x"})
            out = await _rest(
                combiner, "POST", f"/sessions/token/{tok}/filter", {"allowed_servers": []}
            )
            # allowed=[] means "disable everything", not "allow all".
            assert "mockup" in out["disabled_servers"]
            names = {t.name for t in await c.list_tools()}
            assert not any(n.startswith("mockup_") for n in names)

    async def test_pending_filter_bookkeeping_before_connect(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        combiner = await _start_stdio_combiner(procs, tmp_path)
        tok = _token()
        out = await _rest(combiner, "POST", f"/sessions/token/{tok}/filter", {"disable": "mockup"})
        assert out["pending"] is True
        status = await _rest(combiner, "GET", f"/sessions/token/{tok}/filter")
        assert status["session_id"] is None
        assert status["disabled_servers"] == ["mockup"]

    @xfail_session_namespace_split
    async def test_pending_filter_applies_at_first_connect(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        combiner = await _start_stdio_combiner(procs, tmp_path)
        tok = _token()
        await _rest(combiner, "POST", f"/sessions/token/{tok}/filter", {"disable": "mockup"})

        async with _header_client(combiner, tok) as c:
            names = {t.name for t in await c.list_tools()}
            assert not any(n.startswith("mockup_") for n in names)


class TestIsolatedUpstreamSessions:
    async def test_isolate_true_gives_each_chat_its_own_upstream_session(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        tools_path = write_tools_spec(tmp_path / "tools.json", _SPEC)
        mock = await procs.start_http_mock("mockup", tools_path=tools_path)
        cfg = write_servers_config(
            tmp_path / "servers.json",
            {"mockup": {**http_mock_entry(mock.port), "isolate": True}},
        )
        combiner = await procs.start_combiner(cfg)
        await combiner.wait_server_state("mockup", ("ready",))

        tok_a, tok_b = _token(), _token()
        async with (
            _header_client(combiner, tok_a) as ca,
            _header_client(combiner, tok_b) as cb,
        ):
            assert await _text(ca, "mockup_greet", {"who": "a"}) == "Hello, a!"
            assert await _text(cb, "mockup_greet", {"who": "b"}) == "Hello, b!"

        stats = await mock.stats()
        # Each isolated chat reached the upstream over its own MCP session.
        greet_sessions = [sid for sid in stats["session_calls"] if sid != "<none>"]
        assert stats["tool_calls"]["greet"] == 2
        assert len(greet_sessions) >= 2, stats["session_calls"]

    async def test_same_token_reconnect_reattaches_upstream_session(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        """A chat reconnecting under a new downstream session (same token)
        reattaches to its existing upstream session — the token-keyed cache
        survives downstream session churn (mid-chat SSE drop / transport
        cycle) within the grace window."""
        tools_path = write_tools_spec(tmp_path / "tools.json", _SPEC)
        mock = await procs.start_http_mock("mockup", tools_path=tools_path)
        cfg = write_servers_config(
            tmp_path / "servers.json",
            {"mockup": {**http_mock_entry(mock.port), "isolate": True}},
        )
        combiner = await procs.start_combiner(cfg)
        await combiner.wait_server_state("mockup", ("ready",))

        tok = _token()
        async with _header_client(combiner, tok) as c1:
            assert await _text(c1, "mockup_greet", {"who": "a"}) == "Hello, a!"
        # Downstream session fully torn down; same chat token reconnects.
        async with _header_client(combiner, tok) as c2:
            assert await _text(c2, "mockup_greet", {"who": "b"}) == "Hello, b!"

        stats = await mock.stats()
        greet_sessions = {sid for sid in stats["session_calls"] if sid != "<none>"}
        assert stats["tool_calls"]["greet"] == 2
        assert len(greet_sessions) == 1, stats["session_calls"]

    async def test_same_token_concurrent_sessions_share_upstream(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        """Two concurrent downstream sessions bearing the SAME token are one
        chat — they share one isolated upstream session."""
        tools_path = write_tools_spec(tmp_path / "tools.json", _SPEC)
        mock = await procs.start_http_mock("mockup", tools_path=tools_path)
        cfg = write_servers_config(
            tmp_path / "servers.json",
            {"mockup": {**http_mock_entry(mock.port), "isolate": True}},
        )
        combiner = await procs.start_combiner(cfg)
        await combiner.wait_server_state("mockup", ("ready",))

        tok = _token()
        async with (
            _header_client(combiner, tok) as c1,
            _header_client(combiner, tok) as c2,
        ):
            assert await _text(c1, "mockup_greet", {"who": "a"}) == "Hello, a!"
            assert await _text(c2, "mockup_greet", {"who": "b"}) == "Hello, b!"

        stats = await mock.stats()
        greet_sessions = {sid for sid in stats["session_calls"] if sid != "<none>"}
        assert stats["tool_calls"]["greet"] == 2
        assert len(greet_sessions) == 1, stats["session_calls"]

    async def test_park_and_resume_across_grace_expiry(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        """After the grace window the chat's upstream client is parked
        (disconnected without terminating the upstream session); the token's
        next appearance resumes the SAME upstream session via the seeded
        transport."""
        tools_path = write_tools_spec(tmp_path / "tools.json", _SPEC)
        mock = await procs.start_http_mock("mockup", tools_path=tools_path)
        cfg = write_servers_config(
            tmp_path / "servers.json",
            {"mockup": {**http_mock_entry(mock.port), "isolate": True}},
            isolation={"grace_seconds": 0.5, "park_ttl_seconds": 3600},
        )
        combiner = await procs.start_combiner(cfg)
        await combiner.wait_server_state("mockup", ("ready",))

        tok = _token()
        async with _header_client(combiner, tok) as c1:
            assert await _text(c1, "mockup_greet", {"who": "a"}) == "Hello, a!"

        # Let the grace window lapse and the sweeper park the entry.
        import asyncio

        await asyncio.sleep(2.0)

        async with _header_client(combiner, tok) as c2:
            assert await _text(c2, "mockup_greet", {"who": "b"}) == "Hello, b!"

        stats = await mock.stats()
        greet_sessions = {sid for sid in stats["session_calls"] if sid != "<none>"}
        assert stats["tool_calls"]["greet"] == 2
        assert len(greet_sessions) == 1, stats["session_calls"]

    async def test_non_isolated_chats_share_one_upstream_session(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        tools_path = write_tools_spec(tmp_path / "tools.json", _SPEC)
        mock = await procs.start_http_mock("mockup", tools_path=tools_path)
        cfg = write_servers_config(
            tmp_path / "servers.json", {"mockup": http_mock_entry(mock.port)}
        )
        combiner = await procs.start_combiner(cfg)
        await combiner.wait_server_state("mockup", ("ready",))

        tok_a, tok_b = _token(), _token()
        async with (
            _header_client(combiner, tok_a) as ca,
            _header_client(combiner, tok_b) as cb,
        ):
            assert await _text(ca, "mockup_greet", {"who": "a"}) == "Hello, a!"
            assert await _text(cb, "mockup_greet", {"who": "b"}) == "Hello, b!"

        stats = await mock.stats()
        # Default (non-isolated): both chats share the combiner's persistent
        # upstream connection — a single upstream session serves both.
        greet_sessions = {sid for sid in stats["session_calls"] if sid != "<none>"}
        assert stats["tool_calls"]["greet"] == 2
        assert len(greet_sessions) == 1, stats["session_calls"]
