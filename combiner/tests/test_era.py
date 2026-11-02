"""The combiner offers its clients the handshake era only (mcp_combiner.era).

A client that probes ``server/discover`` (fastmcp's default ``mode='auto'``,
Claude Code) must fall back to the stateful ``initialize`` handshake, so it
keeps ``tools/list_changed``, elicitation and per-session broadcasts. The
control run with ``MCP_COMBINER_OFFER_2026=1`` proves the pin is what makes the
difference: the same client then negotiates 2026-07-28.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from conftest import (
    CombinerHandle,
    ProcFactory,
    stdio_mock_entry,
    write_servers_config,
    write_tools_spec,
)
from fakes import content_text, json_object
from fastmcp import Client
from fastmcp.exceptions import ToolError
from mcp.client.subscriptions import listen
from mcp.shared.subscriptions import ToolsListChanged
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS, MODERN_PROTOCOL_VERSIONS

from mcp_combiner.era import OFFER_2026_ENV

pytestmark = pytest.mark.e2e


async def _negotiated(procs: ProcFactory, tmp_path: Path, env: dict[str, str] | None) -> str:
    config = write_servers_config(tmp_path / "servers.json", {})
    combiner = await procs.start_combiner(config, env=env)
    async with Client(combiner.mcp_url) as client:  # default mode="auto": probes discover
        version = client.protocol_version
        assert await client.list_tools() is not None
    assert version is not None
    return str(version)


async def test_auto_client_falls_back_to_the_handshake(procs: ProcFactory, tmp_path: Path) -> None:
    assert await _negotiated(procs, tmp_path, None) in HANDSHAKE_PROTOCOL_VERSIONS


async def test_offer_flag_lets_an_auto_client_negotiate_2026(
    procs: ProcFactory, tmp_path: Path
) -> None:
    version = await _negotiated(procs, tmp_path, {OFFER_2026_ENV: "1"})
    assert version in MODERN_PROTOCOL_VERSIONS


async def test_listen_stream_gets_per_chat_tool_changes_only(
    procs: ProcFactory, tmp_path: Path
) -> None:
    """A 2026-era chat's subscriptions/listen stream receives tools/list_changed
    when its own tool set changes — and another chat's stream does not."""
    config = write_servers_config(tmp_path / "servers.json", {"mockup": stdio_mock_entry("mockup")})
    combiner = await procs.start_combiner(config, env={OFFER_2026_ENV: "1"})
    await combiner.wait_server_state("mockup", ("ready",))

    async with (
        Client(f"{combiner.mcp_url}/chat-aaaaaaaa") as chat_a,
        Client(f"{combiner.mcp_url}/chat-bbbbbbbb") as chat_b,
    ):
        assert chat_a.protocol_version in MODERN_PROTOCOL_VERSIONS
        async with (
            listen(chat_a.session, tools_list_changed=True) as stream_a,
            listen(chat_b.session, tools_list_changed=True) as stream_b,
        ):
            await chat_a.call_tool("combiner__session_disable_server", {"server_name": "mockup"})
            event = await asyncio.wait_for(anext(aiter(stream_a)), timeout=10.0)
            assert isinstance(event, ToolsListChanged)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(anext(aiter(stream_b)), timeout=1.0)
        names = {t.name for t in await chat_a.list_tools()}
        assert not any(n.startswith("mockup_") for n in names)


class _Answer:
    """A 2026-era client's elicitation handler: answers consent asks with a
    fixed choice and counts how often it was asked."""

    def __init__(self, choice: str) -> None:
        self.choice = choice
        self.asked = 0

    async def __call__(
        self, message: str, response_type: Any, params: Any, context: object
    ) -> dict[str, Any]:
        self.asked += 1
        return {"value": self.choice}


async def _consent_combiner(procs: ProcFactory, tmp_path: Path) -> CombinerHandle:
    tools = write_tools_spec(
        tmp_path / "tools.json",
        [{"name": "greet", "params": {"who": "string"}, "response_template": "Hello, {who}!"}],
    )
    entry = stdio_mock_entry("mockup", tools_path=tools)
    entry["permissions"] = {"elicit": ["greet"]}
    config = write_servers_config(tmp_path / "servers.json", {"mockup": entry})
    combiner = await procs.start_combiner(config, env={OFFER_2026_ENV: "1"})
    await combiner.wait_server_state("mockup", ("ready",))
    return combiner


class TestConsentOn2026:
    """Permission consent on the 2026 revision: the call returns an
    InputRequiredResult and the client's retry carries the answer."""

    async def test_allow_once_runs_the_call(self, procs: ProcFactory, tmp_path: Path) -> None:
        combiner = await _consent_combiner(procs, tmp_path)
        answer = _Answer("Allow once")
        async with Client(f"{combiner.mcp_url}/chat-consent1", elicitation_handler=answer) as c:
            assert c.protocol_version in MODERN_PROTOCOL_VERSIONS
            result = await c.call_tool("mockup_greet", {"who": "x"})
            assert content_text(result) == "Hello, x!"
            await c.call_tool("mockup_greet", {"who": "y"})
        assert answer.asked == 2  # "once" is not remembered

    async def test_deny_never_reaches_the_upstream(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        combiner = await _consent_combiner(procs, tmp_path)
        answer = _Answer("Deny")
        async with Client(f"{combiner.mcp_url}/chat-consent2", elicitation_handler=answer) as c:
            with pytest.raises(ToolError, match="denied by the user"):
                await c.call_tool("mockup_greet", {"who": "x"})
            stats = json_object(content_text(await c.call_tool("mockup_mock__stats", {})))
        assert "greet" not in stats["tool_calls"]

    async def test_allow_for_session_asks_once_per_chat(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        combiner = await _consent_combiner(procs, tmp_path)
        answer = _Answer("Allow for session")
        url = f"{combiner.mcp_url}/chat-consent3"
        async with Client(url, elicitation_handler=answer) as c:
            await c.call_tool("mockup_greet", {"who": "x"})
            await c.call_tool("mockup_greet", {"who": "y"})
        async with Client(url, elicitation_handler=answer) as c:  # same chat token
            await c.call_tool("mockup_greet", {"who": "z"})
        assert answer.asked == 1
