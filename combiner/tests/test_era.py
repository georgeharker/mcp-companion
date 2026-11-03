"""The combiner offers its clients the handshake era only (mcp_combiner.era).

A client that probes ``server/discover`` (fastmcp's default ``mode='auto'``,
Claude Code) must fall back to the stateful ``initialize`` handshake, so it
keeps ``tools/list_changed``, elicitation and per-session broadcasts. The
control run with ``MCP_COMBINER_OFFER_2026=1`` proves the pin is what makes the
difference: the same client then negotiates 2026-07-28.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import (
    FAST_TIMING_ENV,
    CombinerHandle,
    ProcFactory,
    http_mock_entry,
    stdio_mock_entry,
    write_servers_config,
    write_tools_spec,
)
from fakes import content_text, json_object
from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult
from fastmcp.exceptions import ToolError
from mcp.client.subscriptions import listen
from mcp.shared.subscriptions import ToolsListChanged
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS, MODERN_PROTOCOL_VERSIONS

from mcp_combiner import upstream_era
from mcp_combiner.config import ServerConfig
from mcp_combiner.era import OFFER_2026_ENV
from mcp_combiner.isolated import REGISTRY, ParkedSession
from mcp_combiner.runtime import CLIENT_CONNECT_MODE

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


ANSWER: dict[str, Any] = {"environment": "prod", "confirm": True, "ticket": "T-1", "replicas": 3}


async def _held_combiner(
    procs: ProcFactory, tmp_path: Path, extra_env: dict[str, str] | None = None
) -> CombinerHandle:
    """A handshake-era isolated upstream behind a combiner offering 2026."""
    mock = await procs.start_http_mock("mockup")
    config = write_servers_config(
        tmp_path / "servers.json",
        {"mockup": {**http_mock_entry(mock.port), "isolate": True}},
        isolation={"grace_seconds": 3600.0, "park_ttl_seconds": 3600.0},
    )
    combiner = await procs.start_combiner(
        config,
        env={**FAST_TIMING_ENV, OFFER_2026_ENV: "1", **(extra_env or {})},
        extra_args=["--log-file", str(tmp_path / "combiner.log")],
    )
    await combiner.wait_server_state("mockup", ("ready",))
    return combiner


def _upstream_saw(result: Any) -> dict[str, Any]:
    text = content_text(result)
    assert text.startswith("elicit: "), text[:200]
    return json_object(text.removeprefix("elicit: "))


class TestHeldCalls:
    """2026-era client + handshake-era upstream that elicits mid-call: the
    combiner holds the upstream call and relays the question as an
    InputRequiredResult; the client's retry answers it."""

    async def test_answer_reaches_the_upstream(self, procs: ProcFactory, tmp_path: Path) -> None:
        combiner = await _held_combiner(procs, tmp_path)
        asked: list[str] = []

        async def answer(message: str, response_type: Any, params: Any, context: object) -> Any:
            asked.append(message)
            return ANSWER

        async with Client(f"{combiner.mcp_url}/chat-held-001", elicitation_handler=answer) as c:
            assert c.protocol_version in MODERN_PROTOCOL_VERSIONS
            payload = _upstream_saw(await c.call_tool("mockup_mock__elicit_form", {}))
        assert asked and "Deploy confirmation" in asked[0]
        assert payload["action"] == "accept"
        assert payload["content"] == ANSWER

    async def test_decline_reaches_the_upstream(self, procs: ProcFactory, tmp_path: Path) -> None:
        combiner = await _held_combiner(procs, tmp_path)

        async def decline(message: str, response_type: Any, params: Any, context: object) -> Any:
            return ElicitResult(action="decline")

        async with Client(f"{combiner.mcp_url}/chat-held-002", elicitation_handler=decline) as c:
            payload = _upstream_saw(await c.call_tool("mockup_mock__elicit_form", {}))
        assert payload["action"] == "decline"

    async def test_an_expired_hold_asks_the_client_to_call_again(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        combiner = await _held_combiner(procs, tmp_path, {"MCP_COMBINER_HOLD_TIMEOUT": "1"})

        async def slow(message: str, response_type: Any, params: Any, context: object) -> Any:
            await asyncio.sleep(2.0)  # the user takes longer than the hold
            return ANSWER

        async with Client(f"{combiner.mcp_url}/chat-held-003", elicitation_handler=slow) as c:
            with pytest.raises(ToolError, match="Call the tool again"):
                await c.call_tool("mockup_mock__elicit_form", {})


# ── upstream era: protocol override + the reverse bridge ───────────


class TestUpstreamEraDecision:
    """What protocol: auto/legacy/2026 resolves to without any probe."""

    async def test_explicit_settings_win(self) -> None:
        legacy = ServerConfig.from_dict(
            "a", {"url": "http://127.0.0.1:1/mcp", "protocol": "legacy"}
        )
        modern = ServerConfig.from_dict("b", {"url": "http://127.0.0.1:1/mcp", "protocol": "2026"})
        assert await upstream_era.resolve("a", legacy) == "legacy"
        assert await upstream_era.resolve("b", modern) == "2026"
        assert upstream_era.client_mode("b") == upstream_era.MODERN_CLIENT_MODE

    async def test_a_2026_upstream_drops_stale_per_chat_sessions(self) -> None:
        # A handover restores parked handshake sessions before eras resolve; a
        # server that turns out to be 2026 must not keep them.
        parked = ParkedSession(session_id="sid-old", protocol_version=None, url="http://x/mcp")
        REGISTRY.restore_parked("e26", "tok-stale", parked)
        REGISTRY.restore_parked("eleg", "tok-stale", parked)
        try:
            modern = ServerConfig.from_dict("e26", {"url": "http://x/mcp", "protocol": "2026"})
            legacy = ServerConfig.from_dict("eleg", {"url": "http://x/mcp", "protocol": "legacy"})
            await upstream_era.resolve("e26", modern)
            await upstream_era.resolve("eleg", legacy)
            assert REGISTRY.pop_parked("e26", "tok-stale") is None
            assert REGISTRY.pop_parked("eleg", "tok-stale") is not None
        finally:
            await REGISTRY.evict_server("eleg")

    async def test_auto_falls_back_for_unprobeable_servers(self) -> None:
        stdio = ServerConfig.from_dict("s", {"command": "true"})
        oauth = ServerConfig.from_dict("o", {"url": "http://127.0.0.1:1/mcp", "auth": "oauth"})
        down = ServerConfig.from_dict("d", {"url": "http://127.0.0.1:1/mcp"})
        for name, srv in (("s", stdio), ("o", oauth), ("d", down)):
            assert await upstream_era.resolve(name, srv) == "legacy", name


async def _reverse_combiner(
    procs: ProcFactory,
    tmp_path: Path,
    *,
    mock_offers_2026: bool,
    protocol: str | None = None,
    offer_downstream: bool = False,
    sessions: bool = False,
) -> CombinerHandle:
    mock = await procs.start_http_mock("mockup", offer_2026=mock_offers_2026, sessions=sessions)
    entry: dict[str, Any] = {**http_mock_entry(mock.port), "isolate": True}
    if protocol is not None:
        entry["protocol"] = protocol
    config = write_servers_config(tmp_path / "servers.json", {"mockup": entry})
    env = {OFFER_2026_ENV: "1"} if offer_downstream else None
    combiner = await procs.start_combiner(
        config, env=env, extra_args=["--log-file", str(tmp_path / "combiner.log")]
    )
    await combiner.wait_server_state("mockup", ("ready",))
    return combiner


def _upstream_era_logged(tmp_path: Path) -> str:
    text = (tmp_path / "combiner.log").read_text()
    found = [era for era in ("2026", "legacy") if f"Upstream 'mockup': {era} protocol" in text]
    assert len(found) == 1, found
    return found[0]


def _guard_saw(result: Any) -> dict[str, Any]:
    text = content_text(result)
    assert text.startswith("guard: "), text[:200]
    return json_object(text.removeprefix("guard: "))


class TestReverseBridge:
    """A 2026-era upstream asks by returning InputRequiredResult."""

    async def test_auto_detects_a_2026_upstream_and_bridges_a_handshake_client(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        combiner = await _reverse_combiner(procs, tmp_path, mock_offers_2026=True)
        assert _upstream_era_logged(tmp_path) == "2026"
        asked: list[str] = []

        async def answer(message: str, response_type: Any, params: Any, context: object) -> Any:
            asked.append(message)
            return ANSWER

        url = f"{combiner.mcp_url}/chat-rev-0001"
        async with Client(url, elicitation_handler=answer, mode=CLIENT_CONNECT_MODE) as c:
            assert c.protocol_version not in MODERN_PROTOCOL_VERSIONS
            payload = _guard_saw(await c.call_tool("mockup_mock__ask_guard", {}))
        assert asked and "Guard confirmation" in asked[0]
        assert payload == {"action": "accept", "content": ANSWER}

    async def test_a_2026_client_reaches_a_2026_upstream_natively(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        combiner = await _reverse_combiner(
            procs, tmp_path, mock_offers_2026=True, offer_downstream=True
        )

        async def answer(message: str, response_type: Any, params: Any, context: object) -> Any:
            return ANSWER

        async with Client(f"{combiner.mcp_url}/chat-rev-0002", elicitation_handler=answer) as c:
            assert c.protocol_version in MODERN_PROTOCOL_VERSIONS
            payload = _guard_saw(await c.call_tool("mockup_mock__ask_guard", {}))
        assert payload == {"action": "accept", "content": ANSWER}

    async def test_a_pinned_upstream_stays_on_the_handshake(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        await _reverse_combiner(procs, tmp_path, mock_offers_2026=False)
        assert _upstream_era_logged(tmp_path) == "legacy"

    async def test_protocol_legacy_overrides_a_2026_upstream(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        await _reverse_combiner(procs, tmp_path, mock_offers_2026=True, protocol="legacy")
        assert _upstream_era_logged(tmp_path) == "legacy"

    async def test_health_checks_keep_a_2026_upstream_connected(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        """ping exists only on the handshake revisions; a 2026 server must be
        health-checked with server/discover, or it is reconnected every few
        intervals (seen live against svg-mcp)."""
        mock = await procs.start_http_mock("mockup", offer_2026=True)
        config = write_servers_config(
            tmp_path / "servers.json", {"mockup": http_mock_entry(mock.port)}
        )
        combiner = await procs.start_combiner(
            config, env=FAST_TIMING_ENV, extra_args=["--log-file", str(tmp_path / "combiner.log")]
        )
        await combiner.wait_server_state("mockup", ("ready",))
        await asyncio.sleep(5)  # five health intervals at FAST_TIMING_ENV
        log = (tmp_path / "combiner.log").read_text()
        assert "Upstream 'mockup': 2026 protocol" in log
        misses = [line for line in log.splitlines() if "Health-check" in line]
        assert not misses, misses[:3]
        assert "Reconnecting to 'mockup'" not in log
        async with Client(f"{combiner.mcp_url}/chat-health-1", mode=CLIENT_CONNECT_MODE) as c:
            assert content_text(await c.call_tool("mockup_mock__whoami", {}))

    async def test_protocol_2026_reaches_an_upstream_that_pins_itself(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        """cribsheet's shape: discover lists nothing, yet a forced 2026 entry
        connects on that revision (the pin only steers auto-negotiation)."""
        combiner = await _reverse_combiner(procs, tmp_path, mock_offers_2026=False, protocol="2026")
        assert _upstream_era_logged(tmp_path) == "2026"

        async def answer(message: str, response_type: Any, params: Any, context: object) -> Any:
            return ANSWER

        url = f"{combiner.mcp_url}/chat-pin-0001"
        async with Client(url, elicitation_handler=answer, mode=CLIENT_CONNECT_MODE) as c:
            # The guard can only ask on a 2026 connection: success proves the era.
            payload = _guard_saw(await c.call_tool("mockup_mock__ask_guard", {}))
        assert payload == {"action": "accept", "content": ANSWER}


# ── discover responses the probe must survive ─────────────────────


class TestParseDiscover:
    MODERN = sorted(MODERN_PROTOCOL_VERSIONS)[-1]

    def _result(self, versions: object) -> str:
        return json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"supportedVersions": versions}})

    def test_json_and_sse_bodies(self) -> None:
        body = self._result([self.MODERN, "2025-11-25"])
        assert upstream_era.parse_discover(200, body) == [self.MODERN, "2025-11-25"]
        sse = f"event: message\ndata: {body}\n\n"
        assert upstream_era.parse_discover(200, sse) == [self.MODERN, "2025-11-25"]

    @pytest.mark.parametrize(
        ("status", "text"),
        [
            (404, "not found"),
            (500, '{"jsonrpc": "2.0", "id": 1, "result": {"supportedVersions": ["x"]}}'),
            (200, "<html>hello</html>"),
            (200, "{not json"),
            (200, "data: {not json"),
            (200, ""),
            (200, '{"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "nope"}}'),
            (200, '{"jsonrpc": "2.0", "id": 1, "result": {}}'),
            (200, '{"jsonrpc": "2.0", "id": 1, "result": {"supportedVersions": "2026-07-28"}}'),
            (200, '["not", "an", "object"]'),
        ],
    )
    def test_odd_responses_list_nothing(self, status: int, text: str) -> None:
        assert upstream_era.parse_discover(status, text) == []

    def test_non_string_versions_are_dropped(self) -> None:
        assert upstream_era.parse_discover(200, self._result([self.MODERN, 7, None])) == [
            self.MODERN
        ]

    async def test_auto_needs_a_modern_version_listed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        srv = ServerConfig.from_dict("probe", {"url": "http://127.0.0.1:1/mcp"})

        async def handshake_only(_srv: ServerConfig) -> list[str]:
            return ["2025-11-25", "2025-06-18"]

        async def modern(_srv: ServerConfig) -> list[str]:
            return ["2025-11-25", self.MODERN]

        monkeypatch.setattr(upstream_era, "_discover", handshake_only)
        assert await upstream_era.resolve("probe", srv) == "legacy"
        monkeypatch.setattr(upstream_era, "_discover", modern)
        assert await upstream_era.resolve("probe", srv) == "2026"


# ── an upstream that owns its sessions (mock --sessions) ──────────


def _text(result: Any) -> str:
    return content_text(result)


async def _mint(client: Client[Any]) -> str:
    result = await client.call_tool("mockup_mock__create_session", {})
    minted = result.structured_content
    if not isinstance(minted, dict) or not isinstance(minted.get("session_id"), str):
        raise AssertionError(f"create_session returned {minted!r}")
    return str(minted["session_id"])


class TestSessionOwningUpstream:
    """The 2026 contract end to end: the upstream mints session ids, the model
    passes them, and the combiner neither isolates nor touches them."""

    async def _start(self, procs: ProcFactory, tmp_path: Path) -> CombinerHandle:
        return await _reverse_combiner(
            procs, tmp_path, mock_offers_2026=True, offer_downstream=True, sessions=True
        )

    async def test_detected_shared_and_session_id_visible(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        combiner = await self._start(procs, tmp_path)
        assert _upstream_era_logged(tmp_path) == "2026"
        assert "isolate is ignored" in (tmp_path / "combiner.log").read_text()
        async with Client(f"{combiner.mcp_url}/chat-own-0001") as c:
            tools = {t.name: t for t in await c.list_tools()}
        assert "mockup_mock__create_session" in tools
        for name in ("mockup_mock__remember", "mockup_mock__recall"):
            assert "session_id" in tools[name].input_schema["properties"], name

    async def test_chats_on_either_era_keep_their_own_sessions(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        combiner = await self._start(procs, tmp_path)
        old = Client(f"{combiner.mcp_url}/chat-own-old1", mode=CLIENT_CONNECT_MODE)
        new = Client(f"{combiner.mcp_url}/chat-own-new1")
        async with old as a, new as b:
            assert a.protocol_version not in MODERN_PROTOCOL_VERSIONS
            assert b.protocol_version in MODERN_PROTOCOL_VERSIONS
            sid_a, sid_b = await _mint(a), await _mint(b)
            await a.call_tool("mockup_mock__remember", {"value": "alpha", "session_id": sid_a})
            await b.call_tool("mockup_mock__remember", {"value": "beta", "session_id": sid_b})
            assert _text(await a.call_tool("mockup_mock__recall", {"session_id": sid_a})) == "alpha"
            assert _text(await b.call_tool("mockup_mock__recall", {"session_id": sid_b})) == "beta"

    async def test_no_session_is_guided_and_ended_ids_are_refused(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        combiner = await self._start(procs, tmp_path)
        async with Client(f"{combiner.mcp_url}/chat-own-0002") as c:
            # Upstream connection is 2026, so without an id there is nowhere to keep state.
            with pytest.raises(ToolError, match="call mock__create_session"):
                await c.call_tool("mockup_mock__remember", {"value": "lost"})
            sid = await _mint(c)
            await c.call_tool("mockup_mock__remember", {"value": "kept", "session_id": sid})
            await c.call_tool("mockup_mock__end_session", {"session_id": sid})
            with pytest.raises(ToolError, match="unknown session_id"):
                await c.call_tool("mockup_mock__recall", {"session_id": sid})

    async def test_sessions_survive_a_combiner_restart(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        """What owning sessions upstream buys: the combiner holds nothing, so a
        new combiner process serves the same session."""
        combiner = await self._start(procs, tmp_path)
        async with Client(f"{combiner.mcp_url}/chat-own-0003") as c:
            sid = await _mint(c)
            await c.call_tool("mockup_mock__remember", {"value": "durable", "session_id": sid})
        combiner.terminate()
        reborn = await procs.start_combiner(combiner.config_path, env={OFFER_2026_ENV: "1"})
        await reborn.wait_server_state("mockup", ("ready",))
        async with Client(f"{reborn.mcp_url}/chat-own-0003") as c:
            assert _text(await c.call_tool("mockup_mock__recall", {"session_id": sid})) == "durable"

    async def test_protocol_legacy_keeps_per_connection_state(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        """Control: the same server forced to the handshake keeps state per
        chat without an id (isolate applies again)."""
        combiner = await _reverse_combiner(
            procs, tmp_path, mock_offers_2026=True, protocol="legacy", sessions=True
        )
        assert _upstream_era_logged(tmp_path) == "legacy"
        one = Client(f"{combiner.mcp_url}/chat-leg-0001", mode=CLIENT_CONNECT_MODE)
        two = Client(f"{combiner.mcp_url}/chat-leg-0002", mode=CLIENT_CONNECT_MODE)
        async with one as a, two as b:
            await a.call_tool("mockup_mock__remember", {"value": "mine"})
            assert _text(await a.call_tool("mockup_mock__recall", {})) == "mine"
            with pytest.raises(ToolError, match="nothing remembered"):
                await b.call_tool("mockup_mock__recall", {})
