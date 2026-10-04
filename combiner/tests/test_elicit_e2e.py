"""E2E: upstream FORM elicitation forwards through the combiner.

The chain under test: a real mockserver subprocess (HTTP upstream, ``isolate:
True`` so the per-chat StatefulProxyClient machinery is active) → the combiner's
FastMCPProxy (fastmcp's context-restoring elicit handler + ProxyTool.run's
``_proxy_rc_ref`` stash) → a downstream fastmcp client whose elicitation
handler ANSWERS the form.

Pins three behaviors:
- the ``requestedSchema`` crosses VERBATIM (the 4-property form: enum + boolean
  + string + integer — "more than y/n" is the point);
- the answered content round-trips back into the upstream tool's echo, keyed as
  ``{action, content}`` — provenance: mock__elicit_form;
- a DECLINE from the human's dialog stays a decline end-to-end (no fabricated
  accept anywhere in the chain).

See notifications.attach_elicitation_forwarding's docstring: the combiner does
NOT wrap this path itself — fastmcp's StatefulProxyClient restoring machinery is
the mechanism; these tests pin THAT wiring through the real proxyfactory path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import (
    FAST_TIMING_ENV,
    CombinerHandle,
    ProcFactory,
    http_mock_entry,
    write_servers_config,
)
from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult
from fastmcp.client.transports import StreamableHttpTransport

pytestmark = pytest.mark.e2e

TOKEN = "e11ce217-0000-4000-8000-000000000001"
EXPECTED_PROPS = ["environment", "confirm", "ticket", "replicas"]

ANSWER: dict[str, Any] = {
    "environment": "prod",
    "confirm": True,
    "ticket": "TEST-42",
    "replicas": 3,
}


async def _started(procs: ProcFactory, tmp_path: Path, port: int | None = None) -> CombinerHandle:
    """Combiner + real HTTP mock upstream, isolate on, fast timing."""
    mock = await procs.start_http_mock("mockup")
    cfg = write_servers_config(
        tmp_path / "servers.json",
        {"mockup": {**http_mock_entry(mock.port), "isolate": True}},
        isolation={"grace_seconds": 3600.0, "park_ttl_seconds": 3600.0},
    )
    combiner = await procs.start_combiner(cfg, port=port, env=FAST_TIMING_ENV)
    await combiner.wait_server_state("mockup", ("ready",))
    return combiner


def _chat(combiner: CombinerHandle, elicitation_handler: Any) -> Client[Any]:
    return Client(
        StreamableHttpTransport(combiner.mcp_url, headers={"X-MCP-Combiner-Session": TOKEN}),
        elicitation_handler=elicitation_handler,
    )


def _text_of(result: Any) -> str:
    """First text block of a CallToolResult (content blocks are a union)."""
    for block in result.content:
        if block.type == "text":
            return str(block.text)
    return ""


def _answering_handler(seen: dict[str, Any]) -> Any:
    """The downstream user's answers: records what arrived, replies with ANSWER."""

    async def handler(message: str, response_type: Any, params: Any, context: Any) -> ElicitResult:
        seen["message"] = message
        schema = getattr(params, "requestedSchema", None) or {}
        seen["props"] = list(schema.get("properties", {}))
        seen["required"] = list(schema.get("required", []))
        # fastmcp contract: a bare dict return IS the content (auto-accept); the
        # envelope shape must be an ElicitResult — action + content verbatim.
        return ElicitResult(action="accept", content=dict(ANSWER))

    return handler


def _declining_handler(seen: dict[str, Any]) -> Any:
    async def handler(message: str, response_type: Any, params: Any, context: Any) -> ElicitResult:
        seen["message"] = message
        return ElicitResult(action="decline")

    return handler


@pytest.mark.e2e
async def test_elicit_form_round_trips_verbatim(procs: ProcFactory, tmp_path: Path) -> None:
    """Full chain: upstream elicit → combiner → the client's dialog answers →
    the upstream tool echoes {action, content} with the answered fields intact."""
    combiner = await _started(procs, tmp_path)
    seen: dict[str, Any] = {}

    async with _chat(combiner, _answering_handler(seen)) as c:
        result = await c.call_tool("mockup_mock__elicit_form", {})

    text = _text_of(result)
    assert text.startswith("elicit: "), f"unexpected tool result: {text[:200]}"
    payload = json.loads(text.removeprefix("elicit: "))
    assert payload["action"] == "accept"
    assert payload["content"] == ANSWER, payload["content"]

    # The FORM crossed verbatim: all four properties, correct order, the
    # required list intact — "more than y/n" survives every hop.
    assert seen["props"] == EXPECTED_PROPS
    assert seen["required"] == ["environment", "confirm"]
    assert "Deploy confirmation" in seen["message"]


@pytest.mark.e2e
async def test_elicit_decline_stays_decline(procs: ProcFactory, tmp_path: Path) -> None:
    """The user's dialog refusal reaches the upstream as decline — no accept is
    fabricated anywhere along the proxy path."""
    combiner = await _started(procs, tmp_path)
    seen: dict[str, Any] = {}

    async with _chat(combiner, _declining_handler(seen)) as c:
        result = await c.call_tool("mockup_mock__elicit_form", {})

    payload = json.loads(_text_of(result).removeprefix("elicit: "))
    assert payload["action"] == "decline"
    assert payload["content"] is None  # decline carries no content


@pytest.mark.e2e
async def test_mock_resources_surface_through_combiner(procs: ProcFactory, tmp_path: Path) -> None:
    """The mock's resource catalog (widget mcp-app + readme text) is visible
    through resources/list — the parity surface the mock hosts for client tests."""
    combiner = await _started(procs, tmp_path)

    async with _chat(combiner, _answering_handler({})) as c:
        listed = await c.list_resources()

    uris = {str(r.uri) for r in listed}
    assert "ui://mockup/mock/widget" in uris, uris
    assert "data://mockup/mock/readme" in uris, uris
