"""E2E upstream restart + self-healing, per transport kind.

Two families:

- ``combiner__restart_server`` (the meta-tool): after restart the proxied call
  must round-trip — i.e. exactly one live provider serves the namespace (the
  process-level regression for COMBINER-RESTART-BUG, which test_mounts.py pins
  at the in-process level) — and for kinds with a combiner-owned process
  (stdio, sharedserver) the backing process is really bounced (boot counter).

- Upstream **crash** (mock__crash): the combiner must self-heal — HTTP
  upstreams reconnect via the health monitor once the process is back;
  sharedserver-backed upstreams escalate to a hard backing-process restart
  after repeated failed re-opens (nothing else respawns them).

Uses FAST_TIMING_ENV so monitor/backoff paths run in seconds.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from conftest import (
    FAST_TIMING_ENV,
    CombinerHandle,
    ProcFactory,
    free_port,
    http_mock_entry,
    poll_until,
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
]


async def _text(client: Client, tool: str, args: dict) -> str:
    result = await client.call_tool(tool, args)
    return result.content[0].text


async def _whoami(client: Client, server: str = "mockup") -> dict:
    return json.loads(await _text(client, f"{server}_mock__whoami", {}))


async def _restart(combiner: CombinerHandle, server: str = "mockup") -> str:
    async with Client(combiner.mcp_url) as c:
        return await _text(c, "combiner__restart_server", {"server_name": server})


async def _wait_pid_gone(pid: int, timeout: float = 15.0) -> None:
    """Wait until a crashed process has really exited — a recovery probe inside
    the pre-crash delay window would "succeed" against the dying process."""
    import os

    async def _gone() -> bool:
        try:
            os.kill(pid, 0)
            return False
        except (ProcessLookupError, PermissionError):
            return True

    await poll_until(_gone, timeout=timeout, desc=f"pid {pid} to exit")


async def _call_until_ok(
    combiner: CombinerHandle, tool: str, args: dict, timeout: float = 45.0
) -> str:
    """Call a tool through the combiner until it succeeds (recovery probe)."""

    async def _try() -> str | None:
        try:
            async with Client(combiner.mcp_url) as c:
                return await _text(c, tool, args)
        except Exception:
            return None

    return await poll_until(_try, timeout=timeout, interval=1.0, desc=f"{tool} to succeed")


class TestRestartMetaTool:
    async def test_stdio_restart_bounces_process(self, procs: ProcFactory, tmp_path: Path) -> None:
        tools_path = write_tools_spec(tmp_path / "tools.json", _SPEC)
        cfg = write_servers_config(
            tmp_path / "servers.json",
            {"mockup": stdio_mock_entry("mockup", tools_path=tools_path, state_dir=tmp_path)},
        )
        combiner = await procs.start_combiner(cfg, env=FAST_TIMING_ENV)
        await combiner.wait_server_state("mockup", ("ready",))

        async with Client(combiner.mcp_url) as c:
            first = await _whoami(c)
        assert first["boot_count"] >= 1

        await _restart(combiner)
        await combiner.wait_server_state("mockup", ("ready",))

        # Round-trip works after restart (no stale provider), on a NEW process.
        async with Client(combiner.mcp_url) as c:
            assert await _text(c, "mockup_greet", {"who": "x"}) == "Hello, x!"
            second = await _whoami(c)
        assert second["boot_count"] > first["boot_count"]
        assert second["pid"] != first["pid"]

    async def test_http_restart_reconnects_same_process(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        tools_path = write_tools_spec(tmp_path / "tools.json", _SPEC)
        mock = await procs.start_http_mock("mockup", tools_path=tools_path)
        cfg = write_servers_config(
            tmp_path / "servers.json", {"mockup": http_mock_entry(mock.port)}
        )
        combiner = await procs.start_combiner(cfg, env=FAST_TIMING_ENV)
        await combiner.wait_server_state("mockup", ("ready",))

        await _restart(combiner)
        await combiner.wait_server_state("mockup", ("ready",))

        # The combiner cannot bounce an external HTTP process: same PID,
        # but the mount must still round-trip.
        async with Client(combiner.mcp_url) as c:
            assert await _text(c, "mockup_greet", {"who": "y"}) == "Hello, y!"
            who = await _whoami(c)
        assert who["pid"] == mock.proc.pid

    async def test_isolated_chat_recovers_after_server_restart(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        """restart_server evicts an isolate:true server's per-chat sessions:
        a tokened chat's next call opens a clean fresh upstream session
        instead of erroring against the dead pre-restart session id, and the
        restart summary makes the state reset legible."""
        from fastmcp.client.transports import StreamableHttpTransport

        tools_path = write_tools_spec(tmp_path / "tools.json", _SPEC)
        mock = await procs.start_http_mock("mockup", tools_path=tools_path)
        cfg = write_servers_config(
            tmp_path / "servers.json",
            {"mockup": {**http_mock_entry(mock.port), "isolate": True}},
        )
        combiner = await procs.start_combiner(cfg, env=FAST_TIMING_ENV)
        await combiner.wait_server_state("mockup", ("ready",))

        token = "11111111-2222-3333-4444-555555555555"

        def _chat() -> Client:
            return Client(
                StreamableHttpTransport(combiner.mcp_url, headers={"X-MCP-Combiner-Session": token})
            )

        async with _chat() as c:
            assert await _text(c, "mockup_greet", {"who": "a"}) == "Hello, a!"

        summary = await _restart(combiner)
        assert "isolated per-chat session(s) were reset" in summary
        await combiner.wait_server_state("mockup", ("ready",))

        # Same chat token: must get a clean fresh upstream session, not a
        # cached client bound to the dead pre-restart session id.
        async with _chat() as c:
            assert await _text(c, "mockup_greet", {"who": "b"}) == "Hello, b!"

    @requires_sharedserver
    async def test_sharedserver_restart_bounces_backing_process(
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
        combiner = await procs.start_combiner(cfg, env=FAST_TIMING_ENV)
        await combiner.wait_server_state("mockup", ("ready",), timeout=60.0)

        async with Client(combiner.mcp_url) as c:
            first = await _whoami(c)

        await _restart(combiner)
        await combiner.wait_server_state("mockup", ("ready",), timeout=60.0)

        async with Client(combiner.mcp_url) as c:
            assert await _text(c, "mockup_greet", {"who": "z"}) == "Hello, z!"
            second = await _whoami(c)
        assert second["boot_count"] > first["boot_count"]
        assert second["pid"] != first["pid"]


class TestCrashSelfHealing:
    async def test_http_upstream_crash_reconnects_when_back(
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
            await c.call_tool("mockup_mock__crash", {"delay_ms": 50})

        # Wait for the process to actually die, then bring a new one up on the
        # same port — the combiner's monitor should re-attach on its own.
        mock.proc.wait(timeout=10)
        await asyncio.sleep(0.5)
        await procs.start_http_mock(
            "mockup", port=mock.port, tools_path=tools_path, state_dir=tmp_path
        )

        text = await _call_until_ok(combiner, "mockup_greet", {"who": "back"})
        assert text == "Hello, back!"
        async with Client(combiner.mcp_url) as c:
            who = await _whoami(c)
        assert who["boot_count"] == 2

    async def test_stdio_upstream_crash_recovers(self, procs: ProcFactory, tmp_path: Path) -> None:
        tools_path = write_tools_spec(tmp_path / "tools.json", _SPEC)
        cfg = write_servers_config(
            tmp_path / "servers.json",
            {"mockup": stdio_mock_entry("mockup", tools_path=tools_path, state_dir=tmp_path)},
        )
        combiner = await procs.start_combiner(cfg, env=FAST_TIMING_ENV)
        await combiner.wait_server_state("mockup", ("ready",))

        async with Client(combiner.mcp_url) as c:
            first = await _whoami(c)
            await c.call_tool("mockup_mock__crash", {"delay_ms": 50})
        await _wait_pid_gone(first["pid"])

        # stdio liveness is lazy: recovery is driven by subsequent calls.
        text = await _call_until_ok(combiner, "mockup_greet", {"who": "again"})
        assert text == "Hello, again!"
        async with Client(combiner.mcp_url) as c:
            second = await _whoami(c)
        assert second["boot_count"] > first["boot_count"]

    @requires_sharedserver
    async def test_sharedserver_crash_escalates_to_backing_restart(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        """Nothing external respawns a crashed sharedserver process; after
        _BACKING_RESTART_AFTER failed re-opens the monitor must hard-restart it."""
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
        combiner = await procs.start_combiner(cfg, env=FAST_TIMING_ENV)
        await combiner.wait_server_state("mockup", ("ready",), timeout=60.0)

        async with Client(combiner.mcp_url) as c:
            first = await _whoami(c)
            await c.call_tool("mockup_mock__crash", {"delay_ms": 50})

        await _wait_pid_gone(first["pid"])

        # No manual respawn here: the combiner itself must escalate.
        text = await _call_until_ok(combiner, "mockup_greet", {"who": "healed"}, timeout=90.0)
        assert text == "Hello, healed!"
        async with Client(combiner.mcp_url) as c:
            second = await _whoami(c)
        assert second["boot_count"] > first["boot_count"]
        assert second["pid"] != first["pid"]
