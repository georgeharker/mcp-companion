"""E2E combiner restart + reconnect to long-lived upstream processes.

The combiner must be restartable without taking its upstreams down with it:
external HTTP processes obviously survive, and sharedserver-backed processes
are refcounted by the external daemon — a combiner stop is an `unuse`, and the
grace period keeps the process alive for the next combiner to re-`use`.

Also pins session behavior across a combiner restart: a client holding a
stale mcp-session-id gets a clean error on the new combiner (not a hang), and
a fresh connect works.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from conftest import (
    FAST_TIMING_ENV,
    ProcFactory,
    free_port,
    http_mock_entry,
    requires_sharedserver,
    sharedserver_def,
    sharedserver_mock_entry,
    unique_shared_name,
    write_servers_config,
    write_tools_spec,
)
from fastmcp import Client

pytestmark = pytest.mark.e2e

_SPEC = [
    {"name": "greet", "params": {"who": "string"}, "response_template": "Hello, {who}!"},
]


async def _text(client: Client, tool: str, args: dict) -> str:
    result = await client.call_tool(tool, args)
    return result.content[0].text


class TestCombinerRestartReattach:
    async def test_reattaches_to_longlived_http_upstream(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        tools_path = write_tools_spec(tmp_path / "tools.json", _SPEC)
        mock = await procs.start_http_mock("mockup", tools_path=tools_path, state_dir=tmp_path)
        cfg = write_servers_config(
            tmp_path / "servers.json", {"mockup": http_mock_entry(mock.port)}
        )

        combiner = await procs.start_combiner(cfg, env=FAST_TIMING_ENV)
        await combiner.wait_server_state("mockup", ("ready",))
        async with Client(combiner.mcp_url) as c:
            assert await _text(c, "mockup_greet", {"who": "one"}) == "Hello, one!"

        # Bounce the combiner; reuse the same port (the Lua plugin's restart
        # flow does the same).
        combiner.terminate()
        await asyncio.sleep(1.0)  # port release
        combiner2 = await procs.start_combiner(cfg, port=combiner.port, env=FAST_TIMING_ENV)
        await combiner2.wait_server_state("mockup", ("ready",))

        async with Client(combiner2.mcp_url) as c:
            assert await _text(c, "mockup_greet", {"who": "two"}) == "Hello, two!"

        # The upstream never restarted.
        stats = await mock.stats()
        assert stats["pid"] == mock.proc.pid
        assert stats["boot_count"] == 1
        assert stats["tool_calls"]["greet"] == 2

    @requires_sharedserver
    async def test_reattaches_to_longlived_sharedserver_upstream(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        """A combiner stop is an `unuse` (refcount drop); the daemon's grace
        period keeps the process alive for the next combiner to re-`use`."""
        tools_path = write_tools_spec(tmp_path / "tools.json", _SPEC)
        port = free_port()
        shared = procs.track_shared(unique_shared_name())
        cfg = write_servers_config(
            tmp_path / "servers.json",
            {"mockup": sharedserver_mock_entry("mockup", shared, port)},
            shared_servers={
                shared: sharedserver_def(
                    shared,
                    port,
                    tools_path=tools_path,
                    state_dir=tmp_path,
                    grace_period="60s",  # long enough to survive the bounce
                )
            },
        )

        combiner = await procs.start_combiner(cfg, env=FAST_TIMING_ENV)
        await combiner.wait_server_state("mockup", ("ready",), timeout=60.0)
        async with Client(combiner.mcp_url) as c:
            assert await _text(c, "mockup_greet", {"who": "one"}) == "Hello, one!"

        async with httpx.AsyncClient() as http:
            first = (await http.get(f"http://127.0.0.1:{port}/stats", timeout=2.0)).json()

        combiner.terminate()
        await asyncio.sleep(1.0)
        combiner2 = await procs.start_combiner(cfg, port=combiner.port, env=FAST_TIMING_ENV)
        await combiner2.wait_server_state("mockup", ("ready",), timeout=60.0)

        async with Client(combiner2.mcp_url) as c:
            assert await _text(c, "mockup_greet", {"who": "two"}) == "Hello, two!"

        async with httpx.AsyncClient() as http:
            second = (await http.get(f"http://127.0.0.1:{port}/stats", timeout=2.0)).json()

        # Same long-lived process across the combiner bounce.
        assert second["pid"] == first["pid"]
        assert second["boot_count"] == first["boot_count"]

    async def test_stale_session_gets_clean_error_not_hang(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        """A client holding a session from the old combiner must get a clean
        failure from the new one (the zombie-session 404 class of bug), and a
        fresh initialize must work."""
        cfg = write_servers_config(
            tmp_path / "servers.json",
            {"mockup": {"command": "python3", "args": [], "disabled": True}},
        )
        # Config note: only need the combiner itself; a disabled entry keeps
        # startup instant.
        combiner = await procs.start_combiner(cfg)

        # Establish a session and capture its id.
        async with httpx.AsyncClient() as http:
            init = await http.post(
                combiner.mcp_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "stale-test", "version": "0"},
                    },
                },
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                },
                timeout=10.0,
            )
            assert init.status_code == 200
            session_id = init.headers.get("mcp-session-id")
            assert session_id

        combiner.terminate()
        await asyncio.sleep(1.0)
        combiner2 = await procs.start_combiner(cfg, port=combiner.port)

        async with httpx.AsyncClient() as http:
            # Stale session id → prompt, clean 4xx (not a hang, not a 200).
            stale = await http.post(
                combiner2.mcp_url,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                    "mcp-session-id": session_id,
                },
                timeout=10.0,
            )
            assert 400 <= stale.status_code < 500

        # A fresh client works immediately.
        async with Client(combiner2.mcp_url) as c:
            tools = await c.list_tools()
            assert any(t.name.startswith("combiner__") for t in tools)
