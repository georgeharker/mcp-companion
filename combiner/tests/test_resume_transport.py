"""Round-trip regression test for ResumableStreamableHttpTransport.

Pins the resume mechanics end-to-end against a real streamable-HTTP MCP
server: initialize a session, disconnect WITHOUT terminating it, reattach
with a seeded transport and no fresh initialize, and observe (from the
upstream side) that both connections rode ONE upstream session. Also pins
the failure contract callers build the fresh-init fallback on: a resume
against an unknown session id raises on first use rather than silently
minting a new session.

This is the drift alarm for the fastmcp/SDK internals the transport mirrors
(see resume_transport.py) — if an upgrade moves the seams, this file fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ProcFactory, write_tools_spec
from fastmcp import Client

from mcp_combiner.resume_transport import ResumableStreamableHttpTransport

pytestmark = pytest.mark.e2e

_SPEC = [
    {"name": "greet", "params": {"who": "string"}, "response_template": "Hello, {who}!"},
]


async def _greet(client: Client, who: str) -> str:
    result = await client.call_tool("greet", {"who": who})
    return result.content[0].text


class TestResumeRoundTrip:
    async def test_seeded_resume_reattaches_to_live_session(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        tools_path = write_tools_spec(tmp_path / "tools.json", _SPEC)
        mock = await procs.start_http_mock("mockup", tools_path=tools_path)

        # First connection: normal initialize, but do NOT terminate the
        # upstream session on close (the park/handover shutdown behavior).
        first = ResumableStreamableHttpTransport(mock.mcp_url, terminate_on_close=False)
        async with Client(first) as c1:
            assert await _greet(c1, "a") == "Hello, a!"
            session_id = first.get_session_id()
            init = c1.initialize_result
            assert session_id, "no upstream session id captured"
            assert init is not None
            protocol_version = str(init.protocolVersion)

        # Second connection: seeded reattach, no initialize handshake.
        second = ResumableStreamableHttpTransport(
            mock.mcp_url,
            resume_session_id=session_id,
            resume_protocol_version=protocol_version,
        )
        async with Client(second, auto_initialize=False) as c2:
            assert await _greet(c2, "b") == "Hello, b!"

        stats = await mock.stats()
        greet_sessions = {sid for sid in stats["session_calls"] if sid != "<none>"}
        assert stats["tool_calls"]["greet"] == 2
        # Both calls arrived on the SAME upstream session — the resume
        # genuinely reattached instead of minting a new session.
        assert greet_sessions == {session_id}, stats["session_calls"]

    async def test_resume_of_unknown_session_raises(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        """The fallback contract: an expired/unknown id fails loudly on first
        use (the caller catches and rebuilds with a fresh initialize)."""
        tools_path = write_tools_spec(tmp_path / "tools.json", _SPEC)
        mock = await procs.start_http_mock("mockup", tools_path=tools_path)

        bogus = ResumableStreamableHttpTransport(
            mock.mcp_url, resume_session_id="00000000000000000000000000000000"
        )
        with pytest.raises(Exception):
            async with Client(bogus, auto_initialize=False) as c:
                await _greet(c, "x")

        # A fresh normal connection still works — the fallback path is open.
        async with Client(ResumableStreamableHttpTransport(mock.mcp_url)) as c:
            assert await _greet(c, "y") == "Hello, y!"
