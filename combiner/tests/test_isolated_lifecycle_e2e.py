"""E2E lifecycle behaviors for token-keyed isolated upstream sessions.

Observed through GET /isolated (the loopback control-plane view of the
live/parked tiers) and mock__remember/mock__recall (a stateful server's
per-session state). Each test pins one decided behavior:

- an explicit downstream session DELETE parks the token's upstream session
  promptly (expedited past the grace window), and the state survives the
  park → resume cycle;
- a silent drop (no DELETE) keeps the client live through the grace window;
- a server that died while a session was parked degrades honestly: the
  resume probe fails, the chat gets a clean fresh session, the state is gone;
- an armed handover followed by a CRASH writes nothing — no crash path can
  produce the file;
- forgetting a parked entry really terminates the upstream session (the
  bare DELETE lands): the forgotten id refuses a direct seeded resume.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import (
    FAST_TIMING_ENV,
    CombinerHandle,
    ProcFactory,
    http_mock_entry,
    poll_until,
    write_servers_config,
    write_tools_spec,
)
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

pytestmark = pytest.mark.e2e

_SPEC = [
    {"name": "greet", "params": {"who": "string"}, "response_template": "Hello, {who}!"},
]

TOKEN = "beefbeef-1111-2222-3333-444444444444"


async def _text(client: Client, tool: str, args: dict) -> str:
    result = await client.call_tool(tool, args)
    return result.content[0].text


def _chat(combiner: CombinerHandle, token: str = TOKEN) -> Client:
    return Client(
        StreamableHttpTransport(combiner.mcp_url, headers={"X-MCP-Combiner-Session": token})
    )


async def _isolated(combiner: CombinerHandle) -> dict[str, Any]:
    async with httpx.AsyncClient() as http:
        r = await http.get(f"{combiner.base_url}/sessions/map", timeout=5.0)
        r.raise_for_status()
        return dict(r.json())


def _entry(state: dict[str, Any], tier: str, server: str, token: str) -> dict[str, Any] | None:
    for entry in state.get(f"isolated_{tier}", []):
        if entry["server"] == server and entry["token"] == token:
            return dict(entry)
    return None


async def _start(
    procs: ProcFactory,
    tmp_path: Path,
    mock_port: int,
    *,
    grace: float,
    ttl: float = 3600.0,
    port: int | None = None,
) -> CombinerHandle:
    cfg = write_servers_config(
        tmp_path / "servers.json",
        {"mockup": {**http_mock_entry(mock_port), "isolate": True}},
        isolation={"grace_seconds": grace, "park_ttl_seconds": ttl},
    )
    combiner = await procs.start_combiner(cfg, port=port, env=FAST_TIMING_ENV)
    await combiner.wait_server_state("mockup", ("ready",))
    return combiner


class TestDeleteVsDrop:
    async def test_clean_close_parks_promptly_and_resume_recalls(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        """Grace is an hour — but a clean close sends a session DELETE, which
        expedites the park. The parked session then resumes with its state."""
        tools_path = write_tools_spec(tmp_path / "tools.json", _SPEC)
        mock = await procs.start_http_mock("mockup", tools_path=tools_path)
        combiner = await _start(procs, tmp_path, mock.port, grace=3600.0)

        async with _chat(combiner) as c:
            await c.call_tool("mockup_mock__remember", {"value": "doc-1"})
        # fastmcp clients terminate their downstream session on clean close
        # (SDK default DELETE) — the middleware expedites the park.

        async def _parked() -> dict[str, Any] | None:
            return _entry(await _isolated(combiner), "parked", "mockup", TOKEN)

        parked = await poll_until(_parked, timeout=10.0, desc="entry to park after DELETE")
        assert parked["upstream_session_id"]

        # The token's next appearance resumes the parked session — with state.
        async with _chat(combiner) as c:
            assert await _text(c, "mockup_mock__recall", {}) == "doc-1"

    async def test_silent_drop_stays_live_through_grace(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        """No DELETE (transport vanishes): the upstream client stays LIVE for
        the whole grace window so a quick reconnect reattaches instantly."""
        from mcp_combiner.resume_transport import ResumableStreamableHttpTransport

        tools_path = write_tools_spec(tmp_path / "tools.json", _SPEC)
        mock = await procs.start_http_mock("mockup", tools_path=tools_path)
        combiner = await _start(procs, tmp_path, mock.port, grace=3600.0)

        # A downstream client that does NOT send the session DELETE on close.
        transport = ResumableStreamableHttpTransport(
            combiner.mcp_url,
            headers={"X-MCP-Combiner-Session": TOKEN},
            terminate_on_close=False,
        )
        async with Client(transport) as c:
            await c.call_tool("mockup_mock__remember", {"value": "doc-2"})

        await asyncio.sleep(3.0)
        state = await _isolated(combiner)
        assert _entry(state, "live", "mockup", TOKEN) is not None
        assert _entry(state, "parked", "mockup", TOKEN) is None

        # Reattaching live keeps the state, of course.
        async with _chat(combiner) as c:
            assert await _text(c, "mockup_mock__recall", {}) == "doc-2"


class TestDegradedModes:
    async def test_server_death_while_parked_falls_back_fresh(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        """The server died while the session was parked: the resume probe
        fails, the chat gets a clean fresh session, and the state is honestly
        gone — no wedged client, no hard error on the tool call."""
        tools_path = write_tools_spec(tmp_path / "tools.json", _SPEC)
        mock = await procs.start_http_mock("mockup", tools_path=tools_path, state_dir=tmp_path)
        combiner = await _start(procs, tmp_path, mock.port, grace=0.5)

        async with _chat(combiner) as c:
            await c.call_tool("mockup_mock__remember", {"value": "doomed"})

        async def _parked() -> dict[str, Any] | None:
            return _entry(await _isolated(combiner), "parked", "mockup", TOKEN)

        await poll_until(_parked, timeout=10.0, desc="entry to park")

        # Kill the mock and bring a new process up on the same port — every
        # session it held is gone.
        mock.terminate()
        await procs.start_http_mock(
            "mockup", port=mock.port, tools_path=tools_path, state_dir=tmp_path
        )

        async def _fresh_greet() -> str | None:
            try:
                async with _chat(combiner) as c:
                    return await _text(c, "mockup_greet", {"who": "back"})
            except Exception:
                return None

        assert await poll_until(_fresh_greet, timeout=45.0, interval=1.0) == "Hello, back!"

        # State is gone — a fresh session has no memory, reported cleanly.
        async with _chat(combiner) as c:
            with pytest.raises(Exception, match="nothing remembered"):
                await c.call_tool("mockup_mock__recall", {})

    async def test_crash_after_arming_writes_no_handover(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        """No crash path can produce the handover file: SIGKILL right after
        arming — the lifespan shutdown never runs, nothing is written."""
        tools_path = write_tools_spec(tmp_path / "tools.json", _SPEC)
        mock = await procs.start_http_mock("mockup", tools_path=tools_path)
        combiner = await _start(procs, tmp_path, mock.port, grace=3600.0)

        handover_file = tmp_path / "handover.json"
        async with httpx.AsyncClient() as http:
            r = await http.post(
                f"{combiner.base_url}/handover/prepare",
                json={"path": str(handover_file)},
                timeout=5.0,
            )
            assert r.status_code == 200

        combiner.proc.kill()  # SIGKILL — no graceful shutdown
        combiner.proc.wait(timeout=10)
        assert not handover_file.exists()

    async def test_forget_terminates_upstream_session(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        """TTL expiry FORGETS a parked entry and its bare DELETE really lands:
        the forgotten upstream session id refuses a direct seeded resume."""
        from mcp_combiner.resume_transport import ResumableStreamableHttpTransport

        tools_path = write_tools_spec(tmp_path / "tools.json", _SPEC)
        mock = await procs.start_http_mock("mockup", tools_path=tools_path)
        combiner = await _start(procs, tmp_path, mock.port, grace=0.5, ttl=3.0)

        async with _chat(combiner) as c:
            await c.call_tool("mockup_mock__remember", {"value": "ephemeral"})

        async def _parked() -> dict[str, Any] | None:
            return _entry(await _isolated(combiner), "parked", "mockup", TOKEN)

        parked = await poll_until(_parked, timeout=10.0, desc="entry to park")
        sid = parked["upstream_session_id"]

        async def _forgotten() -> bool:
            state = await _isolated(combiner)
            return (
                _entry(state, "parked", "mockup", TOKEN) is None
                and _entry(state, "live", "mockup", TOKEN) is None
            )

        await poll_until(_forgotten, timeout=15.0, desc="entry to be forgotten")

        # The upstream session was truly terminated: seeded resume refuses.
        bogus = ResumableStreamableHttpTransport(mock.mcp_url, resume_session_id=sid)
        with pytest.raises(Exception):
            async with Client(bogus, auto_initialize=False) as c:
                await c.call_tool("greet", {"who": "ghost"})
