"""E2E tests for the interactive resource host (mcp-app widgets, Stage 1).

Exercises the /ui/<token>/ route family against a real combiner + mockserver:

- host page render (session creation, CSRF session token, svg-mcp-look chrome)
- proxy auth (session-token check → 401, unknown route → 404)
- widget action path: heartbeat, message/context recording, tool calls through
  the combiner's own /mcp/<token> pipeline (full middleware applies)
- consent gating: deny → 403 on subsequent widget tool calls, grant → allowed
- /events SSE endpoint and the second-origin sandbox relay
- the retrieval loop: combiner__ui_sessions / combiner__ui_messages meta-tools
  return what the widget recorded

The widget HTML is NOT driven in a browser here — the /proxy/* routes are plain
HTTP and the app-bridge bundle is just one client of them, so tests speak the
protocol directly. Browser-level behaviour is the manual matrix (see
docs/designs/interactive-resource-host.md#Testing).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import (
    CombinerHandle,
    ProcFactory,
    stdio_mock_entry,
    write_servers_config,
    write_tools_spec,
)
from fastmcp import Client

pytestmark = pytest.mark.e2e

_SPEC = [
    {"name": "greet", "params": {"who": "string"}, "response_template": "Hello, {who}!"},
]

_WIDGET_URI = "ui://mock/widget"


def _token() -> str:
    return str(uuid.uuid4())


async def _start_combiner(procs: ProcFactory, tmp_path: Path) -> CombinerHandle:
    tools_path = write_tools_spec(tmp_path / "tools.json", _SPEC)
    cfg = write_servers_config(
        tmp_path / "servers.json",
        {"mock": stdio_mock_entry("mock", tools_path=tools_path)},
    )
    combiner = await procs.start_combiner(cfg)
    await combiner.wait_server_state("mock", ("ready",))
    return combiner


async def _open_widget(
    combiner: CombinerHandle, token: str, resource: str
) -> tuple[httpx.Response, str]:
    """GET the host page; return (response, widget session token)."""
    async with httpx.AsyncClient() as http:
        r = await http.get(
            f"{combiner.base_url}/ui/{token}/",
            params={"resource": resource},
            timeout=10.0,
        )
    session_token = r.headers.get("X-MCP-UI-Session", "")
    return r, session_token


async def _proxy(
    combiner: CombinerHandle, token: str, session_token: str, route: str, params: dict[str, Any]
) -> httpx.Response:
    async with httpx.AsyncClient() as http:
        return await http.post(
            f"{combiner.base_url}/ui/{token}/proxy/{route}",
            json={"token": session_token, "params": params},
            timeout=30.0,
        )


def _result_text(result: Any) -> str:
    """First text block of a CallToolResult (content is a union — only read
    the text of TextContent blocks; a non-text first block is a test bug)."""
    for block in result.content:
        if block.type == "text":
            text: str = block.text
            return text
    raise AssertionError(f"no text content in tool result: {result.content}")


async def _widget_tool_name(combiner: CombinerHandle, token: str) -> str:
    """The combiner-namespaced name of the mock's widget ping tool."""
    async with Client(f"{combiner.mcp_url}/{token}") as client:
        tools = await client.list_tools()
    matches = [t.name for t in tools if t.name.endswith("mock__widget_ping")]
    assert matches, f"widget tool not mounted: {[t.name for t in tools]}"
    name: str = matches[0]
    return name


class TestHostPage:
    async def test_host_page_renders_with_session_token_and_chrome(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        combiner = await _start_combiner(procs, tmp_path)
        token = _token()
        r, session_token = await _open_widget(combiner, token, _WIDGET_URI)
        assert r.status_code == 200, r.text
        assert session_token, "host page must mint a widget session token"
        # svg-mcp-family chrome: dark bg + glowing status dot + styled buttons.
        assert "#1a1b1e" in r.text
        assert 'id="dot"' in r.text
        assert "dot.live" in r.text
        # Session + CSRF wiring for the bridge.
        assert session_token in r.text

    async def test_missing_resource_param_is_400(self, procs: ProcFactory, tmp_path: Path) -> None:
        combiner = await _start_combiner(procs, tmp_path)
        token = _token()
        async with httpx.AsyncClient() as http:
            r = await http.get(f"{combiner.base_url}/ui/{token}/", timeout=10.0)
        assert r.status_code == 400

    async def test_unknown_route_is_404(self, procs: ProcFactory, tmp_path: Path) -> None:
        combiner = await _start_combiner(procs, tmp_path)
        token = _token()
        async with httpx.AsyncClient() as http:
            r = await http.get(f"{combiner.base_url}/ui/{token}/nope", timeout=10.0)
        assert r.status_code == 404

    async def test_any_token_gets_a_session(self, procs: ProcFactory, tmp_path: Path) -> None:
        """The grouping token is the capability (it already authorizes /mcp/<t>),
        so the host page creates sessions for any token — a deliberate divergence
        from the design doc's 'unknown token → 404' (see Risks in the design)."""
        combiner = await _start_combiner(procs, tmp_path)
        r, session_token = await _open_widget(combiner, _token(), _WIDGET_URI)
        assert r.status_code == 200
        assert session_token


class TestProxyAuth:
    async def test_wrong_session_token_is_401(self, procs: ProcFactory, tmp_path: Path) -> None:
        combiner = await _start_combiner(procs, tmp_path)
        token = _token()
        _, session_token = await _open_widget(combiner, token, _WIDGET_URI)
        r = await _proxy(combiner, token, "forged-token", "ui/heartbeat", {})
        assert r.status_code == 401

    async def test_heartbeat_touches_session(self, procs: ProcFactory, tmp_path: Path) -> None:
        combiner = await _start_combiner(procs, tmp_path)
        token = _token()
        _, session_token = await _open_widget(combiner, token, _WIDGET_URI)
        r = await _proxy(combiner, token, session_token, "ui/heartbeat", {})
        assert r.status_code == 200
        assert r.json()["ok"] is True


class TestWidgetActions:
    async def test_message_and_context_are_recorded(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        combiner = await _start_combiner(procs, tmp_path)
        token = _token()
        _, session_token = await _open_widget(combiner, token, _WIDGET_URI)

        r = await _proxy(
            combiner,
            token,
            session_token,
            "ui/message",
            {"role": "user", "content": {"type": "text", "text": "hello from widget"}},
        )
        assert r.status_code == 200 and r.json()["ok"] is True
        r = await _proxy(
            combiner,
            token,
            session_token,
            "ui/context",
            {"content": {"type": "text", "text": "ctx"}},
        )
        assert r.status_code == 200

        # The retrieval loop: a chat client reads what the widget recorded.
        async with Client(f"{combiner.mcp_url}/{token}") as client:
            sessions = await client.call_tool("combiner__ui_sessions", {"chat_id": token})
            summary = _result_text(sessions)
            messages = await client.call_tool(
                "combiner__ui_messages", {"chat_id": token, "resource": _WIDGET_URI}
            )
            recorded = _result_text(messages)
        summary_doc = json.loads(summary)
        assert summary_doc["open"] == 1
        assert summary_doc["sessions"][0]["messages"] == 1
        assert summary_doc["sessions"][0]["contexts"] == 1
        assert "hello from widget" in recorded

    async def test_tool_call_runs_through_pipeline_and_is_recorded(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        combiner = await _start_combiner(procs, tmp_path)
        token = _token()
        _, session_token = await _open_widget(combiner, token, _WIDGET_URI)
        tool = await _widget_tool_name(combiner, token)

        r = await _proxy(
            combiner,
            token,
            session_token,
            "tools/call",
            {"name": tool, "arguments": {"msg": "ping"}},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert "widget said: ping" in str(body["result"])

        async with Client(f"{combiner.mcp_url}/{token}") as client:
            messages = await client.call_tool("combiner__ui_messages", {"chat_id": token})
            recorded = _result_text(messages)
        assert tool in recorded
        assert "widget said: ping" in recorded

    async def test_consent_deny_blocks_widget_calls(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        combiner = await _start_combiner(procs, tmp_path)
        token = _token()
        _, session_token = await _open_widget(combiner, token, _WIDGET_URI)
        tool = await _widget_tool_name(combiner, token)

        # Unset consent does not block (the host page ran the widget-side confirm).
        r = await _proxy(
            combiner, token, session_token, "tools/call", {"name": tool, "arguments": {"msg": "a"}}
        )
        assert r.status_code == 200

        # Deny → the widget's calls are refused for this session.
        r = await _proxy(combiner, token, session_token, "ui/consent", {"approved": False})
        assert r.json()["ok"] is True
        r = await _proxy(
            combiner, token, session_token, "tools/call", {"name": tool, "arguments": {"msg": "b"}}
        )
        assert r.status_code == 403

        # Grant → allowed again.
        await _proxy(combiner, token, session_token, "ui/consent", {"approved": True})
        r = await _proxy(
            combiner, token, session_token, "tools/call", {"name": tool, "arguments": {"msg": "c"}}
        )
        assert r.status_code == 200


class TestEventsAndRelay:
    async def test_events_is_an_sse_stream(self, procs: ProcFactory, tmp_path: Path) -> None:
        combiner = await _start_combiner(procs, tmp_path)
        token = _token()
        _, session_token = await _open_widget(combiner, token, _WIDGET_URI)
        async with httpx.AsyncClient(timeout=5.0) as http:
            async with http.stream(
                "GET",
                f"{combiner.base_url}/ui/{token}/events",
                params={"session": session_token},
            ) as r:
                assert r.status_code == 200
                assert r.headers["content-type"].startswith("text/event-stream")
                # Read the first framed event, then hang up — the stream stays
                # open by design (Stage 2 makes it load-bearing).
                first = b""
                async for chunk in r.aiter_bytes():
                    first += chunk
                    if b"\n\n" in first:
                        break
            assert b":" in first or b"data:" in first

    async def test_sandbox_relay_on_second_origin(self, procs: ProcFactory, tmp_path: Path) -> None:
        combiner = await _start_combiner(procs, tmp_path)
        token = _token()
        # The relay binds host-port+1 (see __main__); provider HTML is same-origin
        # with THIS origin, never with the capability-holding host page.
        relay_origin = f"http://127.0.0.1:{combiner.port + 1}"
        async with httpx.AsyncClient() as http:
            r = await http.get(
                f"{relay_origin}/sandbox",
                params={"resource": _WIDGET_URI, "pt": token},
                timeout=10.0,
            )
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "csp" in r.text.lower() or "<html" in r.text.lower()


async def _namespaced_widget_uri(
    combiner: CombinerHandle, token: str, authed: dict[str, str]
) -> str:
    """The combiner-namespaced form of the mock widget resource (upstream
    ui://mock/widget mounts as ui://mock/mock/widget)."""
    from fastmcp.client.transports import StreamableHttpTransport

    async with Client(
        StreamableHttpTransport(f"{combiner.mcp_url}/{token}", headers=authed)
    ) as client:
        resources = await client.list_resources()
    matches = [str(r.uri) for r in resources if str(r.uri).endswith("/widget")]
    assert matches, f"widget resource not mounted: {[str(r.uri) for r in resources]}"
    uri: str = matches[0]
    return uri


async def _widget_tool_name_authed(
    combiner: CombinerHandle, token: str, authed: dict[str, str]
) -> str:
    from fastmcp.client.transports import StreamableHttpTransport

    async with Client(
        StreamableHttpTransport(f"{combiner.mcp_url}/{token}", headers=authed)
    ) as client:
        tools = await client.list_tools()
    matches = [t.name for t in tools if t.name.endswith("mock__widget_ping")]
    assert matches, f"widget tool not mounted: {[t.name for t in tools]}"
    name: str = matches[0]
    return name


class TestInboundAuth:
    """The combiner locked down with MCP_COMBINER_AUTH_TOKEN: the /ui surface
    stays bearer-free (the path token is the capability) but the UI host's
    loopback clients MUST present the bearer on their /mcp/<token> calls —
    the auth middleware does not exempt loopback. Regression: without this,
    every widget action 401s and the host page sticks on "Loading UI..."."""

    async def test_loopback_calls_present_bearer(self, procs: ProcFactory, tmp_path: Path) -> None:
        tools_path = write_tools_spec(tmp_path / "tools.json", _SPEC)
        cfg = write_servers_config(
            tmp_path / "servers.json",
            {"mock": stdio_mock_entry("mock", tools_path=tools_path)},
        )
        combiner = await procs.start_combiner(
            cfg, env={"MCP_COMBINER_AUTH_TOKEN": "ui-test-bearer"}
        )
        await combiner.wait_server_state("mock", ("ready",))
        token = _token()

        authed = {"Authorization": "Bearer ui-test-bearer"}

        # /mcp itself is gated (documents the middleware is actually on). The
        # positive half is covered by the authed Client below — a raw POST needs
        # a full streamable-http handshake, so 400-without-bearer is expected.
        async with httpx.AsyncClient() as http:
            r = await http.post(
                f"{combiner.base_url}/mcp/{token}",
                json={"jsonrpc": "2.0", "method": "ping", "id": 1},
                headers={"Accept": "application/json, text/event-stream"},
                timeout=5.0,
            )
            assert r.status_code == 401

        # The /ui surface itself stays open (token-in-path is the capability).
        widget_uri = await _namespaced_widget_uri(combiner, token, authed)
        r, session_token = await _open_widget(combiner, token, widget_uri)
        assert r.status_code == 200

        # Widget tool calls ride the loopback bearer → 200, not 401.
        tool = await _widget_tool_name_authed(combiner, token, authed)
        r = await _proxy(
            combiner, token, session_token, "tools/call", {"name": tool, "arguments": {"msg": "x"}}
        )
        assert r.status_code == 200, r.text
        assert "widget said: x" in str(r.json()["result"])

        # Resource content via the relay (host-page-driven path) → real HTML.
        async with httpx.AsyncClient() as http:
            r = await http.get(
                f"http://127.0.0.1:{combiner.port + 1}/resource",
                params={"resource": widget_uri, "pt": token},
                timeout=15.0,
            )
        assert r.status_code == 200, r.text
        assert "mock widget" in r.text


class TestStage2Hold:
    """Stage 2: a tool result referencing a widget resource holds the call in
    flight while the user interacts; completion (or timeout) resolves it with
    the recorded state folded into the result."""

    async def _held_call_setup(
        self, procs: ProcFactory, tmp_path: Path, env: dict[str, str] | None = None
    ) -> tuple[CombinerHandle, str, Client[Any], str]:
        tools_path = write_tools_spec(tmp_path / "tools.json", _SPEC)
        cfg = write_servers_config(
            tmp_path / "servers.json",
            {"mock": stdio_mock_entry("mock", tools_path=tools_path)},
        )
        combiner = await procs.start_combiner(cfg, env=env)
        await combiner.wait_server_state("mock", ("ready",))
        token = _token()
        probe = Client(f"{combiner.mcp_url}/{token}probe-probe")  # distinct token
        async with Client(f"{combiner.mcp_url}/{token}") as c:
            tools = await c.list_tools()
        matches = [t.name for t in tools if t.name.endswith("mock__widget_open")]
        assert matches, f"widget-open tool not mounted: {[t.name for t in tools]}"
        tool: str = matches[0]
        return combiner, token, probe, tool

    async def test_call_holds_until_widget_completes(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        combiner, token, probe, tool = await self._held_call_setup(procs, tmp_path)
        widget_uri = await _namespaced_widget_uri(combiner, token, {})

        async with probe, Client(f"{combiner.mcp_url}/{token}") as agent:
            call = asyncio.create_task(agent.call_tool(tool, {}))

            # The hold engages as soon as the result references the widget.
            async def held() -> bool:
                for _ in range(40):
                    s = await probe.call_tool("combiner__ui_sessions", {"chat_id": token})
                    doc = json.loads(_result_text(s))
                    if doc.get("open") == 1:
                        return True
                    await asyncio.sleep(0.25)
                return False

            assert await held(), "call was not held for the widget session"

            # The user opens the widget (host page attaches the same session)...
            r, session_token = await _open_widget(combiner, token, widget_uri)
            assert r.status_code == 200
            assert session_token

            # ...interacts (the SSE stream carries the tool result)...
            async with httpx.AsyncClient(timeout=5.0) as http:
                async with http.stream(
                    "GET",
                    f"{combiner.base_url}/ui/{token}/events",
                    params={"session": session_token},
                ) as stream:
                    seen: list[bytes] = []
                    async for chunk in stream.aiter_bytes():
                        seen.append(chunk)
                        joined = b"".join(seen)
                        if b"event: tool-result" in joined:
                            break
                    assert any(b"tool-input" in c or b"tool-result" in c for c in seen)

            # ...and signals done. The held call resolves with the fold.
            done = await _proxy(combiner, token, session_token, "ui/complete", {"reason": "done"})
            assert done.json()["ok"] is True

            result = await asyncio.wait_for(call, timeout=10.0)
            texts = [b.text for b in result.content if b.type == "text"]
            assert any("completed=done" in t for t in texts), texts
            assert any("/ui/" in t and token in t for t in texts), texts

            # The retrieval loop still sees the session (linger-until-TTL).
            m = await probe.call_tool("combiner__ui_messages", {"chat_id": token})
            doc = json.loads(_result_text(m))
            # The linger-until-TTL design: the completed session is still
            # retrievable even though the holder resolved the call.
            assert doc["token"] == token

    async def test_hold_times_out_and_returns_recorded_state(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        combiner, token, probe, tool = await self._held_call_setup(
            procs, tmp_path, env={"MCP_COMBINER_UI_HOLD_TIMEOUT": "2"}
        )
        widget_uri = await _namespaced_widget_uri(combiner, token, {})
        r, session_token = await _open_widget(combiner, token, widget_uri)
        assert r.status_code == 200

        async with probe, Client(f"{combiner.mcp_url}/{token}") as agent:
            call = asyncio.create_task(agent.call_tool(tool, {}))
            result = await asyncio.wait_for(call, timeout=15.0)
            texts = [b.text for b in result.content if b.type == "text"]
            assert any("completed=timeout" in t for t in texts), texts

            # The widget session completed-by-timeout: done, retrievable.
            s = await probe.call_tool("combiner__ui_sessions", {"chat_id": token})
            doc = json.loads(_result_text(s))
            assert doc["sessions"][0]["completed"] == "timeout"
