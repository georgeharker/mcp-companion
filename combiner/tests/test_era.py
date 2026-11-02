"""The combiner offers its clients the handshake era only (mcp_combiner.era).

A client that probes ``server/discover`` (fastmcp's default ``mode='auto'``,
Claude Code) must fall back to the stateful ``initialize`` handshake, so it
keeps ``tools/list_changed``, elicitation and per-session broadcasts. The
control run with ``MCP_COMBINER_OFFER_2026=1`` proves the pin is what makes the
difference: the same client then negotiates 2026-07-28.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ProcFactory, write_servers_config
from fastmcp import Client
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
