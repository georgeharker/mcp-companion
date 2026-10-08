"""Diagnostic e2e suite for mcp-combiner-issues #1, #4, #5 — written evidence-first.

These tests drive the REAL chain (spawned combiner process + spawned mock upstreams +
real fastmcp streamable-HTTP clients with SSE), so they pin exactly the layers the
historic failures crossed. Each test asserts the CURRENT behavior and prints its
evidence; where a test pins an UNDESIRED behavior (a confirmed issue) it says so and
becomes the regression test for the eventual fix. Marked `diagnostic` in the docstring
of each such case.

- #1  tools invisible until restart_server: does a dynamic enable (combiner
      meta-tool, mid-connection) deliver notifications/tools/list_changed to a
      LIVE streamable-HTTP client session, and when do its tools become visible?
- #4  isolated first-call race: is the FIRST tokened tool call on a fresh/isolate
      path safe, including resume and post-enable windows?
- #5  silent reset: does a combiner restart (meta-tool restart_server and a full
      process restart) wipe per-chat isolated state with ANY signal to the agent?
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx
import mcp.types
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
from fastmcp.client.messages import MessageHandler
from fastmcp.client.transports import StreamableHttpTransport

pytestmark = pytest.mark.e2e

TOKEN = "dededede-1111-2222-3333-444444444444"


def _first_text(result: Any) -> str:
    """First content block as text (CallToolResult content is Text|Image union)."""
    block = result.content[0]
    return block.text if isinstance(block, mcp.types.TextContent) else f"<{type(block).__name__}>"


# ── capture: does tools/list_changed reach a real client over SSE? ────────────────


class _DownstreamCapture(MessageHandler):
    """Records server notifications a REAL combiner client receives.

    The question of record for #1: mid-connection, the combiner broadcasts
    notifications/tools/list_changed on enable. If this capture stays EMPTY while
    tools become visible only via the client's own next poll, delivery is broken —
    visibility then depends entirely on client-side TTLs (native: 5s)."""

    def __init__(self) -> None:
        self.tool_list_changed: list[float] = []

    async def on_tool_list_changed(self, message: mcp.types.ToolListChangedNotification) -> None:
        self.tool_list_changed.append(time.monotonic())


def _chat(
    combiner: CombinerHandle, token: str = TOKEN, cap: _DownstreamCapture | None = None
) -> Client:
    transport = StreamableHttpTransport(
        combiner.mcp_url,
        headers={"X-MCP-Combiner-Session": token},
        sse_read_timeout=120.0,  # the capture needs a LIVE read stream, not an idle-close
    )
    return Client(transport, message_handler=cap)  # type: ignore[arg-type]


async def _names(client: Client) -> set[str]:
    return {t.name for t in await client.list_tools()}


async def _isolated_map(combiner: CombinerHandle) -> dict[str, Any]:
    async with httpx.AsyncClient() as http:
        r = await http.get(f"{combiner.base_url}/sessions/map", timeout=5.0)
        r.raise_for_status()
        return dict(r.json())


# ── #1: dynamic enable → notification + visibility on a live client ───────────────


class TestEnableVisibility:
    async def _setup(
        self, procs: ProcFactory, tmp_path: Path, *, isolate: bool
    ) -> tuple[CombinerHandle, Any, Any]:
        spec_a = write_tools_spec(
            tmp_path / "a.json", [{"name": "greet", "response_template": "hi {who}"}]
        )
        spec_b = write_tools_spec(
            tmp_path / "b.json", [{"name": "wave", "response_template": "wave {who}"}]
        )
        mock_a = await procs.start_http_mock("mockA", tools_path=spec_a)
        mock_b = await procs.start_http_mock("mockB", tools_path=spec_b)
        servers = {
            "mocka": {**http_mock_entry(mock_a.port)},
            # disabled at boot: the enable path is combiner__enable_server
            "mockb": {**http_mock_entry(mock_b.port), "disabled": True},
        }
        if isolate:
            servers["mocka"]["isolate"] = True
            servers["mockb"]["isolate"] = True
        cfg = write_servers_config(tmp_path / "servers.json", servers)
        combiner = await procs.start_combiner(cfg, env=FAST_TIMING_ENV)
        await combiner.wait_server_state("mocka", ("ready",))
        return combiner, mock_a, mock_b

    async def test_enable_mid_session_notifies_and_becomes_visible(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        """#1 primary experiment: enable a disabled server while a REAL streamable-HTTP
        client is connected. Pass requires BOTH (a) a tools/list_changed landing on the
        client's capture and (b) the tools becoming visible. Pin exactly what delivers
        visibility when the notification does not."""
        combiner, _mock_a, _mock_b = await self._setup(procs, tmp_path, isolate=False)
        cap = _DownstreamCapture()
        async with _chat(combiner, cap=cap) as c:
            assert "mockb_wave" not in await _names(c), "disabled server must not advertise tools"

            t0 = time.monotonic()
            await c.call_tool("combiner__enable_server", {"server_name": "mockb"})

            async def _visible() -> bool | None:
                return "mockb_wave" in await _names(c)

            await poll_until(_visible, timeout=15.0, desc="mockb tools visible after enable")
            elapsed = time.monotonic() - t0
            print(f"\n    visibility after enable: {elapsed:.2f}s")
            print(f"    tools/list_changed captured by live client: {len(cap.tool_list_changed)}")

            assert len(cap.tool_list_changed) >= 1, (
                "#1 DIAGNOSIS: enable published but NO tools/list_changed reached the live "
                "streamable-HTTP client — visibility would depend on the client's own TTL. "
                f"captured={cap.tool_list_changed}"
            )

    async def test_enable_isolated_then_first_call_is_safe(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        """#1 × #4 combined: the incident shape — enable an ISOLATED server, then the
        FIRST tokened call happens immediately after (no warm-up). Asserts the call
        succeeds (or records the raw failure for the #4 fix design)."""
        combiner, _mock_a, _mock_b = await self._setup(procs, tmp_path, isolate=True)
        cap = _DownstreamCapture()
        async with _chat(combiner, cap=cap) as c:
            assert "mocka_greet" in await _names(c)
            await c.call_tool("combiner__enable_server", {"server_name": "mockb"})

            # First tokened call on mockb arrives as fast as it can — this IS the race.
            result = await c.call_tool("mockb_wave", {"who": "x"})
            print(f"\n    first isolated call after enable: {_first_text(result)}")
            assert "wave" in _first_text(result)


# ── #4: first tokened call on a fresh chat (no enable involved) ───────────────────


class TestIsolatedFirstCall:
    async def test_first_call_on_fresh_chat_succeeds(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        """#4 want 4 (reproduce on purpose): a FRESH chat's very first action on an
        isolate-true server is a tool call — no priming. The historic window: the
        upstream opened but the isolated session's initialize + first call raced."""
        mock = await procs.start_http_mock("mockup")
        cfg = write_servers_config(
            tmp_path / "servers.json",
            {"mockup": {**http_mock_entry(mock.port), "isolate": True}},
        )
        combiner = await procs.start_combiner(cfg, env=FAST_TIMING_ENV)
        await combiner.wait_server_state("mockup", ("ready",))

        async with _chat(combiner) as c:
            # The FIRST thing this token ever does is a tool call.
            result = await c.call_tool("mockup_echo", {"text": "one"})
            assert "one" in _first_text(result)
            state = await _isolated_map(combiner)
            print(f"\n    sessions/map after first call: {state}")

    async def test_reconnect_then_immediate_call(self, procs: ProcFactory, tmp_path: Path) -> None:
        """#4: the resume path — close+reopen the chat (DELETE parks), then call
        IMMEDIATELY. The parked→resume probe was the other incident window."""
        mock = await procs.start_http_mock("mockup")
        cfg = write_servers_config(
            tmp_path / "servers.json",
            {"mockup": {**http_mock_entry(mock.port), "isolate": True}},
            isolation={"grace_seconds": 3600.0},
        )
        combiner = await procs.start_combiner(cfg, env=FAST_TIMING_ENV)
        await combiner.wait_server_state("mockup", ("ready",))

        async with _chat(combiner) as c:
            await c.call_tool("mockup_echo", {"text": "seed"})
        # clean close → park; now the reopen + immediate call
        async with _chat(combiner) as c2:
            result = await c2.call_tool("mockup_echo", {"text": "resume"})
            assert "resume" in _first_text(result)


# ── #5: reset wipes isolated state — silently? ────────────────────────────────────


class TestResetSilence:
    async def _remember_chat(
        self, procs: ProcFactory, tmp_path: Path, *, isolate: bool
    ) -> CombinerHandle:
        mock = await procs.start_http_mock("mockup")
        servers = {"mockup": {**http_mock_entry(mock.port)}}
        if isolate:
            servers["mockup"]["isolate"] = True
        cfg = write_servers_config(tmp_path / "servers.json", servers)
        return await procs.start_combiner(cfg, env=FAST_TIMING_ENV)

    async def test_meta_restart_resets_isolated_state(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        """#5 DIAGNOSIS (mid-life variant). OBSERVED (evidence, 2026-10-08):

        - ``combiner__restart_server`` REPLACES the token's isolated entry (new
          upstream_session_id) AND ITS RETURN TEXT TELLS THE AGENT: "N isolated
          per-chat session(s) were reset — server-side per-chat state is gone, fresh
          sessions open on next use". Loud, not silent.
        - the first recall after reset hits the fresh session; a LOUD-STATE upstream
          (the mock) surfaces the loss itself as a tool error ("nothing remembered").
          The silence in the original report is specific to SILENT-STATE upstreams
          (svg-mcp: an empty canvas instead of an error).

        Pins the two observable invariants so regressions diff here."""
        combiner = await self._remember_chat(procs, tmp_path, isolate=True)
        async with _chat(combiner) as c:
            await c.call_tool("mockup_mock__remember", {"value": "doc-1"})
            assert _first_text(await c.call_tool("mockup_mock__recall", {})) == "doc-1"

            restart_result = await c.call_tool(
                "combiner__restart_server", {"server_name": "mockup"}
            )
            said = _first_text(restart_result)
            print(f"\n    restart_server said: {said!r}")
            # INVARIANT: the agent-visible reset announcement is part of the return.
            assert "reset" in said and "state is gone" in said, said

            async def _replaced() -> str | None:
                entries = (await _isolated_map(combiner)).get("isolated_live", [])
                for e in entries or []:
                    sid = e.get("upstream_session_id")
                    return str(sid) if sid else None
                return None

            new_id = await poll_until(
                _replaced, timeout=10.0, desc="isolated entry re-established post-reset"
            )
            assert new_id, "fresh upstream session must exist after the reset"

            # First recall on the fresh session — a loud-state upstream surfaces the
            # loss itself (mock errors); the silent-mystery class is svg-mcp-shaped.
            from fastmcp.exceptions import ToolError

            with pytest.raises(ToolError):
                await c.call_tool("mockup_mock__recall", {})
            print(
                "    #5 evidence — post-reset recall raises ToolError (loud upstream); "
                "silent-state upstreams (svg-mcp) remain the residual gap"
            )

    async def test_process_restart_resets_isolated_state(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        """#5 DIAGNOSIS (full variant). OBSERVED (evidence, 2026-10-08):

        The combiner PROCESS restarts WITHOUT handover (kill-path — a crash,
        sharedserver kill, or a manual spawn has no transfer artifact: by the
        no-persist rule, the fresh process has no memory of the pre-restart state).
        The same token reconnects with a fresh upstream session and zero knowledge
        anywhere in the chain that a reset occurred.

        A loud-state upstream surfaces the loss itself (mock recall → tool error
        "nothing remembered in this session"). The original report's SILENT case is
        svg-mcp-shaped (empty canvas, no error) — the residual gap, and the only
        remaining question for the #5 fix (combiner-side tag-on-fresh-session or
        pi-side reconnect note)."""
        combiner = await self._remember_chat(procs, tmp_path, isolate=True)
        port = combiner.port
        cfg_path = combiner.config_path
        async with _chat(combiner) as c:
            await c.call_tool("mockup_mock__remember", {"value": "doc-1"})
            await c.call_tool("mockup_mock__recall", {})
        combiner.terminate()

        # same config, same port: the pi-restart-equivalent from the agent's view
        combiner2 = await procs.start_combiner(cfg_path, port=port, env=FAST_TIMING_ENV)
        await combiner2.wait_server_state("mockup", ("ready",))
        await combiner2.wait_healthy()
        import json as _json

        print(f"\n    health after restart: {_json.dumps((await combiner2.health())['servers'])}")
        receipt = (await combiner2.health()).get("_handover")
        print(f"    _handover receipt: {receipt}")  # kill-path → no transfer artifact
        async with _chat(combiner2) as c2:
            from fastmcp.exceptions import ToolError

            with pytest.raises(ToolError) as err:
                await c2.call_tool("mockup_mock__recall", {})
            print(
                f"    #5 evidence — first recall after process restart raised: "
                f"{type(err.value).__name__}: {err.value}"
            )
