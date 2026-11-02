"""Upstream tool schemas that the MCP wire format rejects (upstream_session).

mcp 2's client validates a ``tools/list`` result against the protocol's wire
models and fails the WHOLE listing over one bad tool schema; the combiner's
``TolerantClientSession`` repairs the raw result first. mcp 2's own server
refuses to send such a schema, so the upstream here is a hand-written
streamable-HTTP JSON-RPC endpoint — the shape a non-Python MCP server with a
sloppy schema really puts on the wire.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from conftest import (
    NoSignalServer,
    ProcFactory,
    free_port,
    http_mock_entry,
    poll_until,
    write_servers_config,
)
from fakes import content_text
from fastmcp import Client
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from mcp_combiner.runtime import CLIENT_CONNECT_MODE
from mcp_combiner.schema import wire_conformant_schema
from mcp_combiner.upstream_session import repair_tool_listing, use_tolerant_session

# What sloppy servers publish: a property given as [] (an empty dict through a
# JSON encoder that can't tell them apart), a non-string `required`, a bare
# non-object output schema.
SLOPPY_TOOLS: list[dict[str, Any]] = [
    {
        "name": "fine",
        "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
    },
    {
        "name": "sloppy",
        "inputSchema": {
            "type": "object",
            "properties": {"opts": [], "q": {"type": "string"}},
            "required": ["q", 3],
        },
        "outputSchema": {"properties": {"n": []}},
    },
]


class TestWireConformantSchema:
    def test_conformant_schema_is_returned_unchanged(self) -> None:
        schema = {"type": "object", "properties": {"a": {"type": "string"}, "b": True}}
        assert wire_conformant_schema(schema) is schema

    def test_repairs(self) -> None:
        assert wire_conformant_schema(
            {"type": "array", "properties": {"opts": [], "ok": {}}, "required": ["ok", 3]}
        ) == {"type": "object", "properties": {"opts": {}, "ok": {}}, "required": ["ok"]}
        assert wire_conformant_schema([]) == {"type": "object", "properties": {}}


class TestRepairToolListing:
    def test_repairs_in_place_and_defaults_a_missing_input_schema(self) -> None:
        raw: dict[str, Any] = {"tools": [*json.loads(json.dumps(SLOPPY_TOOLS)), {"name": "bare"}]}
        repair_tool_listing(raw)
        by_name = {t["name"]: t for t in raw["tools"]}
        assert by_name["fine"] == SLOPPY_TOOLS[0]
        assert by_name["sloppy"]["inputSchema"]["properties"]["opts"] == {}
        assert by_name["sloppy"]["inputSchema"]["required"] == ["q"]
        assert by_name["sloppy"]["outputSchema"] == {
            "type": "object",
            "properties": {"n": {}},
        }
        assert by_name["bare"]["inputSchema"] == {"type": "object", "properties": {}}


# ── e2e: a raw upstream that sends what mcp 2 would never send ─────


def _sloppy_app() -> Starlette:
    """Minimal streamable-HTTP MCP server: JSON responses, no SSE stream."""

    async def mcp(request: Request) -> Response:
        if request.method != "POST":
            return Response(status_code=405)  # no standalone GET stream; DELETE is a no-op
        message = await request.json()
        method, rid = message.get("method"), message.get("id")
        if rid is None:
            return Response(status_code=202)  # a notification
        result: dict[str, Any]
        if method == "initialize":
            result = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "sloppy", "version": "0"},
            }
        elif method == "tools/list":
            result = {"tools": SLOPPY_TOOLS}
        elif method == "tools/call":
            name = message["params"]["name"]
            result = {"content": [{"type": "text", "text": f"called {name}"}]}
            if name == "sloppy":
                result["structuredContent"] = {"n": 1}
        elif method == "ping":
            result = {}
        else:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": method}}
            )
        return JSONResponse(
            {"jsonrpc": "2.0", "id": rid, "result": result},
            headers={"mcp-session-id": "sloppy-session"},
        )

    return Starlette(routes=[Route("/mcp", mcp, methods=["GET", "POST", "DELETE"])])


@pytest.fixture
async def sloppy_upstream() -> AsyncIterator[int]:
    port = free_port()
    server = NoSignalServer(
        uvicorn.Config(_sloppy_app(), host="127.0.0.1", port=port, log_level="warning")
    )
    task = asyncio.create_task(server.serve())

    async def _started() -> bool:
        return server.started

    await poll_until(_started, timeout=10.0, desc="sloppy upstream")
    yield port
    server.should_exit = True
    with contextlib.suppress(Exception):
        await asyncio.wait_for(task, timeout=5.0)


@pytest.mark.e2e
class TestSloppyUpstream:
    async def test_plain_sdk_client_rejects_the_listing(self, sloppy_upstream: int) -> None:
        """The control: without the repair the whole listing fails — this is
        the mcp 2 behaviour the tolerant session exists to absorb."""
        url = f"http://127.0.0.1:{sloppy_upstream}/mcp"
        async with Client(url, mode=CLIENT_CONNECT_MODE) as client:
            with pytest.raises(Exception):
                await client.list_tools()

    async def test_tolerant_session_lists_every_tool(self, sloppy_upstream: int) -> None:
        url = f"http://127.0.0.1:{sloppy_upstream}/mcp"
        client = use_tolerant_session(Client(url, mode=CLIENT_CONNECT_MODE))
        async with client:
            tools = {t.name: t for t in await client.list_tools()}
        assert set(tools) == {"fine", "sloppy"}
        assert tools["sloppy"].input_schema["properties"]["opts"] == {}

    async def test_combiner_serves_and_calls_the_sloppy_tool(
        self, procs: ProcFactory, sloppy_upstream: int, tmp_path: Path
    ) -> None:
        """End to end: the combiner lists the upstream's tools (repaired) and
        publishes a conformant list itself; the repaired tool is callable."""
        cfg = write_servers_config(
            tmp_path / "servers.json", {"sloppy": http_mock_entry(sloppy_upstream)}
        )
        combiner = await procs.start_combiner(cfg)
        await combiner.wait_server_state("sloppy", ("ready",))
        async with Client(combiner.mcp_url, mode=CLIENT_CONNECT_MODE) as client:
            names = {t.name for t in await client.list_tools()}
            assert {"sloppy_fine", "sloppy_sloppy"} <= names
            result = await client.call_tool("sloppy_sloppy", {"q": "x"})
        assert content_text(result) == "called sloppy"
