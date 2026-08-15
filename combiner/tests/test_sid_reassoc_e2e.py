"""PROPOSAL VALIDATION — wire-id chat identity + 404-correlation aliasing.

These tests validate the design proposal that the downstream wire
``Mcp-Session-Id`` suffices as the tokenless tier's chat identity across a
sanctioned combiner restart (see the design graph: "Pivot … PROPOSAL").
The mechanism: the client's first post-restart request presents the dead old
id and 404s; the middleware stashes (client addr → old id); the re-initialize
claims it — by addr when the SDK reuses the connection, else by unambiguous
singleton — and the restored parked state is renamed to the new identity.

Covered:
- singleton-fallback re-association (fastmcp client: re-init opens a NEW
  connection, so addr correlation misses and the singleton path carries it);
- addr-match re-association (raw JSON-RPC over ONE pooled httpx client —
  the 404 and the re-initialize ride the same source port, which is the path
  a keep-alive SDK like Claude Code's should hit);
- the degraded case: two tokenless chats reconnecting concurrently must NOT
  cross-associate — ambiguity fails open to fresh sessions for both.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import (
    CombinerHandle,
    ProcFactory,
    free_port,
    http_mock_entry,
    write_servers_config,
    write_tools_spec,
)
from fastmcp import Client

pytestmark = pytest.mark.e2e

_SPEC = [
    {"name": "greet", "params": {"who": "string"}, "response_template": "Hello, {who}!"},
]


async def _arm_and_restart(
    procs: ProcFactory, combiner: CombinerHandle, cfg: Path, handover: Path
) -> CombinerHandle:
    async with httpx.AsyncClient() as http:
        r = await http.post(
            f"{combiner.base_url}/handover/prepare", json={"path": str(handover)}, timeout=5.0
        )
        assert r.status_code == 200
    combiner.terminate()
    assert handover.exists()
    successor = await procs.start_combiner(
        cfg, port=combiner.port, extra_args=["--restore", str(handover)]
    )
    await successor.wait_server_state("mockup", ("ready",))
    return successor


async def _start_isolated_mock(procs: ProcFactory, tmp_path: Path):
    tools_path = write_tools_spec(tmp_path / "tools.json", _SPEC)
    mock = await procs.start_http_mock("mockup", tools_path=tools_path)
    cfg = write_servers_config(
        tmp_path / "servers.json",
        {"mockup": {**http_mock_entry(mock.port), "isolate": True}},
    )
    port = free_port()
    combiner = await procs.start_combiner(cfg, port=port)
    await combiner.wait_server_state("mockup", ("ready",))
    return mock, cfg, combiner


class TestSingletonReassociation:
    async def test_tokenless_chat_state_survives_restart(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        mock, cfg, combiner = await _start_isolated_mock(procs, tmp_path)
        url = combiner.mcp_url

        # Chat opens (no token — the wire session id is its identity) and
        # holds its connection across the restart, like a live client does.
        c = Client(url)
        async with c:
            await c.call_tool("mockup_mock__remember", {"value": "tokenless-doc"})

            successor = await _arm_and_restart(
                procs, combiner, cfg, tmp_path / "handover.json"
            )

            # The next call presents the dead old id → 404s → the middleware
            # stashes the identity. The call itself fails; the client's
            # reconnect follows.
            with pytest.raises(Exception):
                await c.call_tool("mockup_mock__recall", {})

        # Reconnect (fresh fastmcp context = new connection = addr miss →
        # the unambiguous singleton claim carries the identity).
        async with Client(successor.mcp_url) as c2:
            r = await c2.call_tool("mockup_mock__recall", {})
            assert r.content[0].text == "tokenless-doc"

    async def test_concurrent_reconnects_do_not_cross_associate(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        """Two tokenless chats reconnecting in the same window: ambiguity
        must fail open — fresh sessions for both, never swapped state."""
        mock, cfg, combiner = await _start_isolated_mock(procs, tmp_path)
        url = combiner.mcp_url

        a, b = Client(url), Client(url)
        async with a, b:
            await a.call_tool("mockup_mock__remember", {"value": "state-A"})
            await b.call_tool("mockup_mock__remember", {"value": "state-B"})

            successor = await _arm_and_restart(
                procs, combiner, cfg, tmp_path / "handover.json"
            )

            # Both present their dead ids → two pending identities.
            with pytest.raises(Exception):
                await a.call_tool("mockup_mock__recall", {})
            with pytest.raises(Exception):
                await b.call_tool("mockup_mock__recall", {})

        # Both reconnect on new connections: addr misses, singleton refuses
        # (two candidates). Each must land on a FRESH session — a recall that
        # returns the OTHER chat's value would be cross-association.
        for who in ("first", "second"):
            async with Client(successor.mcp_url) as c:
                with pytest.raises(Exception, match="nothing remembered"):
                    await c.call_tool("mockup_mock__recall", {})


class _RawMcp:
    """Minimal MCP-over-streamable-HTTP client on ONE pooled httpx client.

    Exists to pin the addr-match path: with connection keep-alive, the 404
    for the old id and the subsequent initialize ride the same source port —
    the correlation a real SDK's reconnect should produce.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self.http = httpx.AsyncClient(
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            timeout=10.0,
        )
        self.sid: str | None = None
        self._next_id = 0

    async def close(self) -> None:
        await self.http.aclose()

    @staticmethod
    def _parse_body(resp: httpx.Response) -> dict[str, Any] | None:
        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            for line in resp.text.splitlines():
                if line.startswith("data:"):
                    return dict(json.loads(line[5:].strip()))
            return None
        if resp.content:
            return dict(resp.json())
        return None

    async def request(self, method: str, params: dict[str, Any]) -> httpx.Response:
        self._next_id += 1
        headers = {}
        if self.sid:
            headers["mcp-session-id"] = self.sid
        return await self.http.post(
            self.url,
            json={"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params},
            headers=headers,
        )

    async def initialize(self) -> None:
        self.sid = None
        resp = await self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "raw-test", "version": "0"},
            },
        )
        resp.raise_for_status()
        self.sid = resp.headers.get("mcp-session-id")
        assert self.sid, "no session id minted"
        r = await self.http.post(
            self.url,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={"mcp-session-id": self.sid},
        )
        assert r.status_code in (200, 202)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        resp = await self.request("tools/call", {"name": name, "arguments": arguments})
        resp.raise_for_status()
        body = self._parse_body(resp)
        assert body is not None, "no response body"
        return body


class TestAddrMatchReassociation:
    async def test_same_connection_reinit_reclaims_identity(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        mock, cfg, combiner = await _start_isolated_mock(procs, tmp_path)

        raw = _RawMcp(combiner.mcp_url)
        try:
            await raw.initialize()
            body = await raw.call_tool("mock" + "up_mock__remember", {"value": "raw-doc"})
            assert "error" not in body, body

            successor = await _arm_and_restart(
                procs, combiner, cfg, tmp_path / "handover.json"
            )
            raw.url = successor.mcp_url  # same host/port; same pooled client

            # Old id presented on the kept-alive pool → 404 stashes the
            # identity with OUR source address.
            resp = await raw.request("tools/call", {"name": "mockup_mock__recall", "arguments": {}})
            assert resp.status_code == 404, resp.status_code

            # Re-initialize on the SAME pooled connection → addr match →
            # the new identity inherits the parked state.
            await raw.initialize()
            body = await raw.call_tool("mockup_mock__recall", {})
            text = json.dumps(body)
            assert "raw-doc" in text, body
        finally:
            await raw.close()
