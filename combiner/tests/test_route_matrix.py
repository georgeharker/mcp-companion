"""E2E route matrix: a real combiner subprocess in front of mock upstreams,
one per transport kind (stdio, raw HTTP, sharedserver-backed HTTP).

For each route: the upstream's tools appear under its namespace, a tools/call
round-trips with the right payload, and /health reports the server ready.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import (
    ProcFactory,
    free_port,
    http_mock_entry,
    requires_sharedserver,
    sharedserver_def,
    sharedserver_mock_entry,
    stdio_mock_entry,
    unique_shared_name,
    write_servers_config,
    write_tools_spec,
)
from fastmcp import Client

pytestmark = pytest.mark.e2e

_SPEC = [
    {"name": "greet", "params": {"who": "string"}, "response_template": "Hello, {who}!"},
    {
        "name": "broken",
        "input_schema": {"type": "object", "properties": {"x": {}}, "required": ["missing"]},
        "response_template": "still callable",
    },
]


async def _text(client: Client, tool: str, args: dict) -> str:
    result = await client.call_tool(tool, args)
    return result.content[0].text


class TestStdioRoute:
    async def test_stdio_upstream_round_trip(self, procs: ProcFactory, tmp_path: Path) -> None:
        tools_path = write_tools_spec(tmp_path / "tools.json", _SPEC)
        cfg = write_servers_config(
            tmp_path / "servers.json",
            {"mockup": stdio_mock_entry("mockup", tools_path=tools_path)},
        )
        combiner = await procs.start_combiner(cfg)
        await combiner.wait_server_state("mockup", ("ready",))

        async with Client(combiner.mcp_url) as c:
            names = {t.name for t in await c.list_tools()}
            assert "mockup_greet" in names
            assert await _text(c, "mockup_greet", {"who": "stdio"}) == "Hello, stdio!"
            who = json.loads(await _text(c, "mockup_mock__whoami", {}))
            assert who["server_name"] == "mockup"

    async def test_malformed_schema_survives_combiner(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        """A tool publishing a broken schema still lists and calls through the
        combiner (whose sanitizer must not drop or crash on it)."""
        tools_path = write_tools_spec(tmp_path / "tools.json", _SPEC)
        cfg = write_servers_config(
            tmp_path / "servers.json",
            {"mockup": stdio_mock_entry("mockup", tools_path=tools_path)},
        )
        combiner = await procs.start_combiner(cfg)
        await combiner.wait_server_state("mockup", ("ready",))

        async with Client(combiner.mcp_url) as c:
            tools = {t.name: t for t in await c.list_tools()}
            assert "mockup_broken" in tools
            assert await _text(c, "mockup_broken", {}) == "still callable"


class TestHttpRoute:
    async def test_http_upstream_round_trip(self, procs: ProcFactory, tmp_path: Path) -> None:
        tools_path = write_tools_spec(tmp_path / "tools.json", _SPEC)
        mock = await procs.start_http_mock("mockup", tools_path=tools_path)
        cfg = write_servers_config(
            tmp_path / "servers.json", {"mockup": http_mock_entry(mock.port)}
        )
        combiner = await procs.start_combiner(cfg)
        await combiner.wait_server_state("mockup", ("ready",))

        async with Client(combiner.mcp_url) as c:
            names = {t.name for t in await c.list_tools()}
            assert "mockup_greet" in names
            assert await _text(c, "mockup_greet", {"who": "http"}) == "Hello, http!"

        # The combiner reached the same process we spawned.
        stats = await mock.stats()
        assert stats["pid"] == mock.proc.pid
        assert stats["tool_calls"].get("greet") == 1

    async def test_health_reports_transport_states(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        mock = await procs.start_http_mock("up")
        cfg = write_servers_config(
            tmp_path / "servers.json",
            {
                "up": http_mock_entry(mock.port),
                "down": http_mock_entry(free_port()),  # nothing listening
                "off": {**http_mock_entry(mock.port), "disabled": True},
            },
        )
        combiner = await procs.start_combiner(cfg)
        await combiner.wait_server_state("up", ("ready",))

        health = await combiner.health()
        servers = health["servers"]
        assert servers["up"]["state"] == "ready"
        assert servers["off"]["state"] == "disabled"
        assert servers["down"]["state"] not in ("ready", "disabled")


@requires_sharedserver
class TestSharedServerRoute:
    async def test_sharedserver_upstream_round_trip(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        tools_path = write_tools_spec(tmp_path / "tools.json", _SPEC)
        port = free_port()
        shared = procs.track_shared(unique_shared_name())
        cfg = write_servers_config(
            tmp_path / "servers.json",
            {"mockup": sharedserver_mock_entry("mockup", shared, port)},
            shared_servers={
                shared: sharedserver_def(shared, port, tools_path=tools_path, state_dir=tmp_path)
            },
        )
        combiner = await procs.start_combiner(cfg)
        await combiner.wait_server_state("mockup", ("ready",), timeout=60.0)

        async with Client(combiner.mcp_url) as c:
            assert await _text(c, "mockup_greet", {"who": "shared"}) == "Hello, shared!"
