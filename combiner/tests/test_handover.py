"""Sanctioned-restart handover: unit round-trip + full restart e2e.

The handover is a one-shot transfer artifact, not persistence: written only
by a shutdown that ctl armed via POST /handover/prepare, consumed-and-deleted
by the successor's --restore, refused on version/staleness mismatch. The e2e
proves the headline behavior: a tokened chat's isolated upstream session and
its token-keyed filter survive `stop → start --restore` — the backing server
never died, and the successor resumes the same upstream session id.
"""

from __future__ import annotations

import json
import stat
import time
from pathlib import Path

import httpx
import pytest
from conftest import (
    ProcFactory,
    free_port,
    http_mock_entry,
    write_servers_config,
    write_tools_spec,
)
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from mcp_combiner import runtime
from mcp_combiner.ctl import _strip_flag_with_value
from mcp_combiner.handover import HANDOVER_VERSION, load_handover, write_handover
from mcp_combiner.isolated import REGISTRY, ParkedSession
from mcp_combiner.runtime import RUNTIME

_SPEC = [
    {"name": "greet", "params": {"who": "string"}, "response_template": "Hello, {who}!"},
]


@pytest.fixture(autouse=True)
def _clean_runtime():
    runtime.reset()
    yield
    runtime.reset()


class TestRoundTrip:
    async def test_write_then_load_restores_state(self, tmp_path: Path) -> None:
        RUNTIME.sessions.set_pending("tok-1", {"github", "svg-mcp"})
        REGISTRY.restore_parked(
            "svg-mcp",
            "tok-1",
            ParkedSession(
                session_id="sid-xyz",
                protocol_version="2025-06-18",
                url="http://127.0.0.1:9/mcp",
                headers={"x": "y"},
            ),
        )
        path = tmp_path / "handover.json"
        await write_handover(str(path))

        # Mode 600: the file carries tokens.
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

        runtime.reset()
        assert not RUNTIME.sessions.pending_token_filters
        assert not REGISTRY.parked

        assert load_handover(str(path)) is True
        assert not path.exists(), "consumed file must be deleted"
        assert RUNTIME.sessions.pending_token_filters == {"tok-1": {"github", "svg-mcp"}}
        parked = REGISTRY.pop_parked("svg-mcp", "tok-1")
        assert parked is not None
        assert parked.session_id == "sid-xyz"
        assert parked.protocol_version == "2025-06-18"
        assert parked.headers == {"x": "y"}

    async def test_version_mismatch_refused_and_deleted(self, tmp_path: Path) -> None:
        path = tmp_path / "handover.json"
        path.write_text(json.dumps({"version": HANDOVER_VERSION + 1, "created_at": time.time()}))
        assert load_handover(str(path)) is False
        assert not path.exists()

    async def test_stale_snapshot_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "handover.json"
        path.write_text(json.dumps({"version": HANDOVER_VERSION, "created_at": time.time() - 3600}))
        assert load_handover(str(path)) is False
        assert not path.exists()

    def test_missing_file_boots_fresh(self, tmp_path: Path) -> None:
        assert load_handover(str(tmp_path / "nope.json")) is False


class TestCtlHelpers:
    def test_strip_flag_with_value(self) -> None:
        argv = ["mcp-combiner", "--mcp", "--restore", "/a/b.json", "--port", "9741"]
        assert _strip_flag_with_value(argv, "--restore") == [
            "mcp-combiner",
            "--mcp",
            "--port",
            "9741",
        ]
        # No-op when absent; strips repeated pairs.
        assert _strip_flag_with_value(["x", "y"], "--restore") == ["x", "y"]
        assert _strip_flag_with_value(["--restore", "a", "z", "--restore", "b"], "--restore") == [
            "z"
        ]


@pytest.mark.e2e
class TestHandoverE2E:
    async def test_isolated_session_and_filter_survive_sanctioned_restart(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        tools_path = write_tools_spec(tmp_path / "tools.json", _SPEC)
        mock = await procs.start_http_mock("mockup", tools_path=tools_path)
        cfg = write_servers_config(
            tmp_path / "servers.json",
            {"mockup": {**http_mock_entry(mock.port), "isolate": True}},
        )
        port = free_port()
        combiner = await procs.start_combiner(cfg, port=port)
        await combiner.wait_server_state("mockup", ("ready",))

        token = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

        def _chat(c) -> Client:
            return Client(
                StreamableHttpTransport(c.mcp_url, headers={"X-MCP-Combiner-Session": token})
            )

        async with _chat(combiner) as c:
            assert await _text_of(await c.call_tool("mockup_greet", {"who": "a"})) == "Hello, a!"

        # A token-keyed filter for a second (never-connected) token: pure
        # token-keyed state that must ride the handover.
        async with httpx.AsyncClient() as http:
            r = await http.post(
                f"{combiner.base_url}/sessions/token/other-token/filter",
                json={"disable": "mockup"},
                timeout=5.0,
            )
            r.raise_for_status()

        # Arm the handover, then a graceful stop (SIGTERM → lifespan writes it).
        handover_file = tmp_path / "handover.json"
        async with httpx.AsyncClient() as http:
            r = await http.post(
                f"{combiner.base_url}/handover/prepare",
                json={"path": str(handover_file)},
                timeout=5.0,
            )
            assert r.status_code == 200
        combiner.terminate()
        assert handover_file.exists(), "flagged shutdown must write the handover"

        # Successor on the same port consumes the file.
        successor = await procs.start_combiner(
            cfg, port=port, extra_args=["--restore", str(handover_file)]
        )
        await successor.wait_server_state("mockup", ("ready",))
        assert not handover_file.exists(), "restore must consume the file"

        # The same chat token resumes the SAME upstream session (the backing
        # server never died — only the combiner restarted).
        async with _chat(successor) as c:
            assert await _text_of(await c.call_tool("mockup_greet", {"who": "b"})) == "Hello, b!"

        stats = await mock.stats()
        greet_sessions = {sid for sid in stats["session_calls"] if sid != "<none>"}
        assert stats["tool_calls"]["greet"] == 2
        assert len(greet_sessions) == 1, stats["session_calls"]

        # And the token-keyed filter is already in place on the successor —
        # both as bookkeeping and as ENFORCEMENT: the chat that presents that
        # token gets the filtered view from its very first request
        # (read-through — there is no join step to miss).
        async with httpx.AsyncClient() as http:
            r = await http.get(
                f"{successor.base_url}/sessions/token/other-token/filter", timeout=5.0
            )
            r.raise_for_status()
            assert r.json()["disabled_servers"] == ["mockup"]
        async with Client(
            StreamableHttpTransport(
                successor.mcp_url, headers={"X-MCP-Combiner-Session": "other-token"}
            )
        ) as fc:
            names = {t.name for t in await fc.list_tools()}
            assert not any(str(n).startswith("mockup_") for n in names)

    async def test_stateful_server_state_visible_after_sanctioned_restart(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        """The headline: a stateful server's per-session state (svg-mcp's
        current document, a jupyter kernel — modeled by mock__remember) is
        still THERE for the same chat after `mcp-combiner restart`. The
        server never died; only the combiner's plumbing did, and the handover
        carried the session id across."""
        tools_path = write_tools_spec(tmp_path / "tools.json", _SPEC)
        mock = await procs.start_http_mock("mockup", tools_path=tools_path)
        cfg = write_servers_config(
            tmp_path / "servers.json",
            {"mockup": {**http_mock_entry(mock.port), "isolate": True}},
        )
        port = free_port()
        combiner = await procs.start_combiner(cfg, port=port)
        await combiner.wait_server_state("mockup", ("ready",))

        token = "cafecafe-1111-2222-3333-444444444444"

        def _chat(c) -> Client:
            return Client(
                StreamableHttpTransport(c.mcp_url, headers={"X-MCP-Combiner-Session": token})
            )

        async with _chat(combiner) as c:
            await c.call_tool("mockup_mock__remember", {"value": "the-current-document"})

        handover_file = tmp_path / "handover.json"
        async with httpx.AsyncClient() as http:
            r = await http.post(
                f"{combiner.base_url}/handover/prepare",
                json={"path": str(handover_file)},
                timeout=5.0,
            )
            assert r.status_code == 200
        combiner.terminate()

        successor = await procs.start_combiner(
            cfg, port=port, extra_args=["--restore", str(handover_file)]
        )
        await successor.wait_server_state("mockup", ("ready",))

        async with _chat(successor) as c:
            recalled = await _text_of(await c.call_tool("mockup_mock__recall", {}))
        assert recalled == "the-current-document"

    async def test_tokenless_chat_is_amnesiac_after_restart(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        """The custody principle, pinned: a tokenless chat's identity is the
        wire session id, which is maintained by the combiner and therefore
        dies with it. After a sanctioned restart the chat reconnects cleanly
        but on a FRESH session — no state carryover, and no inference-based
        re-association (the 404-correlation scheme was prototyped and
        rejected; see the design graph). Only presented, externally
        maintained identity — a token — survives the restart."""
        tools_path = write_tools_spec(tmp_path / "tools.json", _SPEC)
        mock = await procs.start_http_mock("mockup", tools_path=tools_path)
        cfg = write_servers_config(
            tmp_path / "servers.json",
            {"mockup": {**http_mock_entry(mock.port), "isolate": True}},
        )
        port = free_port()
        combiner = await procs.start_combiner(cfg, port=port)
        await combiner.wait_server_state("mockup", ("ready",))

        c = Client(combiner.mcp_url)  # no token — sid-keyed identity
        async with c:
            await c.call_tool("mockup_mock__remember", {"value": "doomed"})

            handover_file = tmp_path / "handover.json"
            async with httpx.AsyncClient() as http:
                r = await http.post(
                    f"{combiner.base_url}/handover/prepare",
                    json={"path": str(handover_file)},
                    timeout=5.0,
                )
                assert r.status_code == 200
            combiner.terminate()
            successor = await procs.start_combiner(
                cfg, port=port, extra_args=["--restore", str(handover_file)]
            )
            await successor.wait_server_state("mockup", ("ready",))

            # The held connection's next call presents the dead id and fails.
            with pytest.raises(Exception):
                await c.call_tool("mockup_mock__recall", {})

        # Reconnect: clean fresh session, state honestly gone.
        async with Client(successor.mcp_url) as c2:
            with pytest.raises(Exception, match="nothing remembered"):
                await c2.call_tool("mockup_mock__recall", {})


async def _text_of(result) -> str:
    return result.content[0].text
