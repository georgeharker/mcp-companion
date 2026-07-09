"""Tests for the mock MCP server instrument itself (in-process, fast tier).

The e2e matrix (combiner + mock as subprocesses) lives in test_route_matrix.py
and friends; this file pins the instrument's own contract: spec parsing,
verbatim schema publication, response scripting, fault injection, runtime
catalog mutation, and the boot counter.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp import Client

from mcp_combiner.mockserver import MockServer, ToolSpec, parse_tool_specs


def _spec(entries: list[dict]) -> list[ToolSpec]:
    return parse_tool_specs(json.dumps(entries))


async def _text(client: Client, tool: str, args: dict) -> str:
    result = await client.call_tool(tool, args)
    return result.content[0].text


class TestSpecParsing:
    def test_missing_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="missing 'name'"):
            _spec([{"params": {"x": "string"}}])

    def test_unknown_param_type_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown type"):
            _spec([{"name": "t", "params": {"x": "banana"}}])

    def test_unknown_error_mode_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown error_mode"):
            _spec([{"name": "t", "error_mode": "sometimes"}])

    def test_params_shorthand_builds_object_schema(self) -> None:
        (spec,) = _spec([{"name": "t", "params": {"x": "string", "n": "integer"}}])
        assert spec.input_schema == {
            "type": "object",
            "properties": {"x": {"type": "string"}, "n": {"type": "integer"}},
            "required": ["x", "n"],
        }

    def test_raw_input_schema_wins_and_is_kept_verbatim(self) -> None:
        malformed = {"type": "object", "required": 123, "properties": {"x": {}}}
        (spec,) = _spec([{"name": "t", "input_schema": malformed, "params": {"y": "string"}}])
        assert spec.input_schema == malformed

    def test_tools_wrapper_dict_accepted(self) -> None:
        specs = parse_tool_specs(json.dumps({"tools": [{"name": "a"}, {"name": "b"}]}))
        assert [s.name for s in specs] == ["a", "b"]


class TestDefaultCatalog:
    async def test_echo_add_parity(self) -> None:
        srv = MockServer("m", "http")
        async with Client(srv.mcp) as c:
            assert await _text(c, "echo", {"message": "hi"}) == "Echo: hi"
            assert await _text(c, "add", {"a": 2, "b": 3}) == "Sum: 5"

    async def test_instrumentation_tools_present(self) -> None:
        srv = MockServer("m", "http")
        async with Client(srv.mcp) as c:
            names = {t.name for t in await c.list_tools()}
        assert {
            "mock__whoami",
            "mock__stats",
            "mock__sleep",
            "mock__crash",
            "mock__add_tool",
            "mock__remove_tool",
            "mock__push_responses",
            "mock__set_behavior",
        } <= names


class TestSchemaPublication:
    async def test_malformed_schema_published_verbatim(self) -> None:
        malformed = {"type": "object", "required": ["missing"], "properties": {"x": {}}}
        srv = MockServer("m", "http", _spec([{"name": "bad", "input_schema": malformed}]))
        async with Client(srv.mcp) as c:
            tools = {t.name: t for t in await c.list_tools()}
        assert tools["bad"].inputSchema == malformed

    async def test_output_schema_published_verbatim(self) -> None:
        out = {"type": "object", "properties": {"weird": []}}
        srv = MockServer(
            "m", "http", _spec([{"name": "t", "output_schema": out, "response_template": "x"}])
        )
        async with Client(srv.mcp) as c:
            tools = {t.name: t for t in await c.list_tools()}
        assert tools["t"].outputSchema == out


class TestResponses:
    async def test_scripted_fifo_then_template_fallback(self) -> None:
        srv = MockServer(
            "m",
            "http",
            _spec(
                [
                    {
                        "name": "s",
                        "params": {"q": "string"},
                        "responses": [{"text": "first"}, {"json": {"n": 1}}, {"error": "boom"}],
                        "response_template": "fallback: {q}",
                    }
                ]
            ),
        )
        async with Client(srv.mcp) as c:
            assert await _text(c, "s", {"q": "a"}) == "first"
            assert json.loads(await _text(c, "s", {"q": "b"})) == {"n": 1}
            with pytest.raises(Exception, match="boom"):
                await c.call_tool("s", {"q": "c"})
            assert await _text(c, "s", {"q": "d"}) == "fallback: d"

    async def test_template_computed_keys(self) -> None:
        srv = MockServer(
            "m",
            "http",
            _spec(
                [
                    {
                        "name": "t",
                        "params": {"a": "number", "b": "number"},
                        "response_template": "sum={_sum} args={_args}",
                    }
                ]
            ),
        )
        async with Client(srv.mcp) as c:
            text = await _text(c, "t", {"a": 1.5, "b": 2.5})
        assert text.startswith("sum=4.0 args=")

    async def test_bad_template_falls_back_to_json_dump(self) -> None:
        srv = MockServer(
            "m",
            "http",
            _spec([{"name": "t", "params": {"x": "string"}, "response_template": "{nope}"}]),
        )
        async with Client(srv.mcp) as c:
            payload = json.loads(await _text(c, "t", {"x": "v"}))
        assert payload == {"tool": "t", "args": {"x": "v"}}


class TestFaultInjection:
    async def test_error_mode_first_n(self) -> None:
        srv = MockServer(
            "m",
            "http",
            _spec(
                [
                    {
                        "name": "f",
                        "params": {"x": "integer"},
                        "response_template": "ok {x}",
                        "error_mode": "first_n",
                        "error_n": 2,
                    }
                ]
            ),
        )
        async with Client(srv.mcp) as c:
            for i in range(2):
                with pytest.raises(Exception, match="injected error"):
                    await c.call_tool("f", {"x": i})
            assert await _text(c, "f", {"x": 9}) == "ok 9"

    async def test_error_mode_after_n(self) -> None:
        srv = MockServer(
            "m",
            "http",
            _spec(
                [
                    {
                        "name": "f",
                        "params": {"x": "integer"},
                        "response_template": "ok {x}",
                        "error_mode": "after_n",
                        "error_n": 1,
                    }
                ]
            ),
        )
        async with Client(srv.mcp) as c:
            assert await _text(c, "f", {"x": 1}) == "ok 1"
            with pytest.raises(Exception, match="injected error"):
                await c.call_tool("f", {"x": 2})

    async def test_set_behavior_toggles_errors(self) -> None:
        srv = MockServer("m", "http")
        async with Client(srv.mcp) as c:
            await c.call_tool("mock__set_behavior", {"name": "echo", "error_mode": "always"})
            with pytest.raises(Exception, match="injected error"):
                await c.call_tool("echo", {"message": "x"})
            await c.call_tool("mock__set_behavior", {"name": "echo", "error_mode": "none"})
            assert await _text(c, "echo", {"message": "y"}) == "Echo: y"


class TestRuntimeCatalog:
    async def test_add_call_remove(self) -> None:
        srv = MockServer("m", "http")
        async with Client(srv.mcp) as c:
            spec = {"name": "late", "params": {"z": "integer"}, "response_template": "late {z}"}
            await c.call_tool("mock__add_tool", {"spec": json.dumps(spec)})
            assert "late" in {t.name for t in await c.list_tools()}
            assert await _text(c, "late", {"z": 7}) == "late 7"
            await c.call_tool("mock__remove_tool", {"name": "late"})
            assert "late" not in {t.name for t in await c.list_tools()}

    async def test_add_replaces_existing(self) -> None:
        srv = MockServer("m", "http")
        async with Client(srv.mcp) as c:
            spec = {"name": "echo", "params": {"message": "string"}, "response_template": "v2!"}
            await c.call_tool("mock__add_tool", {"spec": json.dumps(spec)})
            assert await _text(c, "echo", {"message": "x"}) == "v2!"

    async def test_push_responses(self) -> None:
        srv = MockServer("m", "http")
        async with Client(srv.mcp) as c:
            await c.call_tool(
                "mock__push_responses",
                {"name": "echo", "responses": json.dumps([{"text": "queued"}])},
            )
            assert await _text(c, "echo", {"message": "x"}) == "queued"
            assert await _text(c, "echo", {"message": "x"}) == "Echo: x"


class TestInstrumentation:
    async def test_whoami_counters_and_sessions(self) -> None:
        srv = MockServer("m", "http")
        async with Client(srv.mcp) as c:
            await c.call_tool("echo", {"message": "1"})
            who = json.loads(await _text(c, "mock__whoami", {}))
        assert who["server_name"] == "m"
        assert who["calls_total"] == 2
        assert who["tool_calls"]["echo"] == 1
        assert len(who["session_ids_seen"]) == 1

    async def test_boot_counter_persists(self, tmp_path: Path) -> None:
        srv1 = MockServer("m", "http", state_dir=tmp_path)
        assert srv1.state.boot_count == 1
        srv2 = MockServer("m", "http", state_dir=tmp_path)
        assert srv2.state.boot_count == 2
        # Independent per name
        other = MockServer("other", "http", state_dir=tmp_path)
        assert other.state.boot_count == 1
