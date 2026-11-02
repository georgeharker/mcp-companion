"""Live tests: the locally installed Claude Code against a throwaway combiner.

Opt-in only (``MCP_COMBINER_LIVE_CLAUDE=1``, with the Agent SDK added at run
time — it is deliberately NOT a project dependency, so neither the lockfile nor
the nix build ever carries it); never in CI. Each test starts its own mock
upstream and combiner (own ports, own bearer token), then drives **your
installed** ``claude`` through the Claude Agent SDK (``cli_path`` — the SDK's
bundled CLI may lag) with a fixed prompt on a cheap model. Elicitations are
answered by a scripted ``Elicitation`` hook, so no dialog is shown. Each run
makes a few real model calls.

Assertions come from the server side: the tool results Claude received (what
the upstream actually saw), and the protocol revision Claude negotiated, read
from the combiner's debug request log.

    MCP_COMBINER_LIVE_CLAUDE=1 uv run --with 'claude-agent-sdk>=0.2.176' \
        pytest tests/test_live_claude.py -v
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from conftest import (
    FAST_TIMING_ENV,
    CombinerHandle,
    ProcFactory,
    http_mock_entry,
    write_servers_config,
    write_tools_spec,
)

from mcp_combiner.era import OFFER_2026_ENV

pytestmark = [
    pytest.mark.live_claude,
    pytest.mark.skipif(
        os.environ.get("MCP_COMBINER_LIVE_CLAUDE") != "1",
        reason="live Claude test: set MCP_COMBINER_LIVE_CLAUDE=1 (makes real model calls)",
    ),
    pytest.mark.skipif(shutil.which("claude") is None, reason="no `claude` on PATH"),
]

sdk: Any = pytest.importorskip(
    "claude_agent_sdk", reason="run with `uv run --with 'claude-agent-sdk>=0.2.176'`"
)

MODEL = os.environ.get("MCP_COMBINER_LIVE_MODEL", "claude-haiku-5-5")
BEARER = "live-claude-test-bearer"
SERVER = "combiner"  # the MCP server name Claude Code sees
ANSWER = {"environment": "prod", "confirm": True, "ticket": "LIVE-1", "replicas": 3}
GREET = [{"name": "greet", "params": {"who": "string"}, "response_template": "Hello, {who}!"}]


async def _combiner(
    procs: ProcFactory,
    tmp_path: Path,
    *,
    offer_2026: bool,
    permissions: dict[str, Any] | None = None,
) -> CombinerHandle:
    mock = await procs.start_http_mock(
        "mockup", tools_path=write_tools_spec(tmp_path / "tools.json", GREET)
    )
    entry: dict[str, Any] = {**http_mock_entry(mock.port), "isolate": True}
    if permissions is not None:
        entry["permissions"] = permissions
    config = write_servers_config(tmp_path / "servers.json", {"mockup": entry})
    env = {**FAST_TIMING_ENV, "MCP_COMBINER_AUTH_TOKEN": BEARER}
    if offer_2026:
        env[OFFER_2026_ENV] = "1"
    combiner = await procs.start_combiner(
        config,
        env=env,
        extra_args=["--log-file", str(tmp_path / "combiner.log"), "--log-level", "debug"],
    )  # /health is open: the default health wait needs no bearer
    await combiner.wait_server_state("mockup", ("ready",))
    return combiner


def _elicitation_hook(tmp_path: Path, answer: dict[str, Any]) -> str:
    """Claude Code settings JSON with an Elicitation hook that answers *answer*."""
    reply = {"hookSpecificOutput": {"hookEventName": "Elicitation", **answer}}
    script = tmp_path / "answer_elicitation.py"
    script.write_text(
        # The reply travels as a Python string literal of its JSON (embedding the
        # JSON itself as code would turn true/false/null into NameErrors).
        f"import sys\nsys.stdin.read()\nsys.stdout.write({json.dumps(json.dumps(reply))})\n"
    )
    hook = {"type": "command", "command": f"{sys.executable} -I {script}"}
    return json.dumps({"hooks": {"Elicitation": [{"matcher": SERVER, "hooks": [hook]}]}})


async def _ask_claude(
    combiner: CombinerHandle,
    tool: str,
    arguments: dict[str, Any],
    *,
    token: str,
    settings: str | None = None,
) -> list[str]:
    """Have Claude call *tool* once with *arguments*; the tool results it got."""
    qualified = f"mcp__{SERVER}__{tool}"
    options = sdk.ClaudeAgentOptions(
        cli_path=shutil.which("claude"),
        model=MODEL,
        max_turns=4,
        setting_sources=[],
        settings=settings,
        allowed_tools=[qualified],
        mcp_servers={
            SERVER: {
                "type": "http",
                "url": f"{combiner.mcp_url}/{token}",
                "headers": {"Authorization": f"Bearer {BEARER}"},
            }
        },
    )
    prompt = (
        f"Call the tool `{qualified}` exactly once with these arguments: "
        f"{json.dumps(arguments)}. Do not call any other tool. Then reply DONE."
    )
    results: list[str] = []
    async for message in sdk.query(prompt=prompt, options=options):
        if isinstance(message, sdk.UserMessage) and isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, sdk.ToolResultBlock):
                    results.append(_block_text(block.content))
    return results


def _block_text(content: str | Sequence[dict[str, Any]] | None) -> str:
    """A tool result's text. Claude hands a structured result through as its
    JSON (``{"result": "..."}``, fastmcp's wrapped form): unwrap that."""
    if content is None:
        return ""
    text = (
        content
        if isinstance(content, str)
        else "".join(str(part.get("text", "")) for part in content)
    )
    try:
        wrapped = json.loads(text)
    except ValueError:
        return text
    result = wrapped.get("result") if isinstance(wrapped, dict) else None
    return result if isinstance(result, str) else text


_VERSION = re.compile(r"'mcp-protocol-version': '([^']+)'")
_METHOD = re.compile(r"'mcp-method': '([^']+)'")


def _requests(log: Path) -> list[tuple[str, str | None]]:
    """(protocol version, 2026-era method header) per request Claude sent,
    from the combiner's debug request log."""
    out: list[tuple[str, str | None]] = []
    for line in log.read_text().splitlines():
        version = _VERSION.search(line)
        if version is not None:
            method = _METHOD.search(line)
            out.append((version.group(1), method.group(1) if method else None))
    return out


def _negotiated(log: Path) -> set[str]:
    """Protocol revisions Claude's requests carried."""
    return {version for version, _ in _requests(log)}


def _methods(log: Path) -> set[str]:
    return set(re.findall(r"'mcp-method': '([^']+)'", log.read_text()))


class TestLiveClaude:
    async def test_offered_2026_claude_negotiates_it(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        combiner = await _combiner(procs, tmp_path, offer_2026=True)
        results = await _ask_claude(combiner, "mockup_greet", {"who": "live"}, token="live-0001")
        assert any("Hello, live!" in r for r in results), results
        log = tmp_path / "combiner.log"
        assert _negotiated(log) == {"2026-07-28"}, _negotiated(log)
        assert {"server/discover", "subscriptions/listen"} <= _methods(log)

    async def test_pinned_claude_falls_back_to_the_handshake(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        combiner = await _combiner(procs, tmp_path, offer_2026=False)
        results = await _ask_claude(combiner, "mockup_greet", {"who": "pinned"}, token="live-0002")
        assert any("Hello, pinned!" in r for r in results), results
        # Claude probes server/discover with the 2026 header; the pin answers
        # "no versions" and every other request then rides the handshake.
        requests = _requests(tmp_path / "combiner.log")
        after_probe = [(v, m) for v, m in requests if m != "server/discover"]
        assert after_probe, requests
        assert all(v != "2026-07-28" for v, _ in after_probe), requests

    async def test_upstream_elicitation_is_held_and_answered(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        """2026 Claude + handshake upstream eliciting mid-call: the combiner
        holds the call; Claude's Elicitation hook answers; the upstream sees it."""
        combiner = await _combiner(procs, tmp_path, offer_2026=True)
        settings = _elicitation_hook(tmp_path, {"action": "accept", "content": ANSWER})
        results = await _ask_claude(
            combiner, "mockup_mock__elicit_form", {}, token="live-0003", settings=settings
        )
        echoed = [r for r in results if r.startswith("elicit: ")]
        assert echoed, results
        payload = json.loads(echoed[0].removeprefix("elicit: "))
        assert payload["action"] == "accept"
        assert payload["content"] == ANSWER

    async def test_consent_prompt_on_2026(self, procs: ProcFactory, tmp_path: Path) -> None:
        """Permission consent as InputRequiredResult, answered by the hook."""
        combiner = await _combiner(
            procs, tmp_path, offer_2026=True, permissions={"elicit": ["greet"]}
        )
        settings = _elicitation_hook(
            tmp_path, {"action": "accept", "content": {"value": "Allow once"}}
        )
        results = await _ask_claude(
            combiner, "mockup_greet", {"who": "consent"}, token="live-0004", settings=settings
        )
        assert any("Hello, consent!" in r for r in results), results
