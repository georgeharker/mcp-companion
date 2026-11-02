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

import pytest
from conftest import ProcFactory, stdio_mock_entry, write_servers_config
from fastmcp import Client
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
