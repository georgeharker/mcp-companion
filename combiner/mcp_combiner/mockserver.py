"""Instrumentable mock MCP server for exercising the combiner in tests.

Runs a FastMCP server over stdio or streamable HTTP with a configurable tool
catalog, plus always-present instrumentation tools that let tests observe
process identity (pid / boot count), per-session traffic, and inject faults
(latency, errors, hard crashes).

Usage:
    python -m mcp_combiner.mockserver --name mock [--transport stdio|http]
        [--host 127.0.0.1 --port 9760] [--tools SPEC.json] [--state-dir DIR]

The three combiner upstream modes map onto this as:
  - stdio upstream:        {"command": "python", "args": ["-m", "mcp_combiner.mockserver", ...]}
  - raw HTTP upstream:     run with --transport http, point config "url" at it
  - sharedserver upstream: same HTTP invocation, launched via a sharedServers entry

Tool spec format (JSON file, or "-" for stdin): a list (or {"tools": [...]})
of entries:
    {
      "name": "greet",
      "description": "Say hello",
      "params": {"who": "string"},            # name -> string|integer|number|boolean
      "response_template": "Hello, {who}!",   # str.format over the call args
      "latency_ms": 0,                        # sleep before responding
      "error_mode": "none",                   # none|always|first_n|after_n
      "error_n": 0                            # n for first_n / after_n
    }

Without --tools, the catalog defaults to `echo` and `add` (parity with the
old test_simple_server.py smoke server).
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import JSONResponse

_PARAM_TYPES: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
}

_ERROR_MODES = ("none", "always", "first_n", "after_n")


@dataclass
class ToolSpec:
    """One configurable tool parsed from the --tools JSON spec."""

    name: str
    description: str = ""
    params: dict[str, str] = field(default_factory=lambda: {"message": "string"})
    response_template: str = "{message}"
    latency_ms: float = 0.0
    error_mode: str = "none"
    error_n: int = 0

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ToolSpec:
        name = raw.get("name")
        if not name or not isinstance(name, str):
            raise ValueError(f"tool spec missing 'name': {raw!r}")
        params = raw.get("params", {"message": "string"})
        for pname, ptype in params.items():
            if ptype not in _PARAM_TYPES:
                raise ValueError(
                    f"tool {name!r}: param {pname!r} has unknown type {ptype!r} "
                    f"(expected one of {sorted(_PARAM_TYPES)})"
                )
        error_mode = raw.get("error_mode", "none")
        if error_mode not in _ERROR_MODES:
            raise ValueError(
                f"tool {name!r}: unknown error_mode {error_mode!r} (expected one of {_ERROR_MODES})"
            )
        return cls(
            name=name,
            description=raw.get("description", f"Mock tool {name}"),
            params=params,
            response_template=raw.get(
                "response_template", "{" + next(iter(params), "message") + "}"
            ),
            latency_ms=float(raw.get("latency_ms", 0)),
            error_mode=error_mode,
            error_n=int(raw.get("error_n", 0)),
        )


@dataclass
class MockState:
    """Shared observable state for the instrumentation tools/routes."""

    server_name: str
    transport: str
    state_dir: Path | None
    start_time: float = field(default_factory=time.time)
    boot_count: int = 1
    calls_total: int = 0
    tool_calls: dict[str, int] = field(default_factory=dict)
    session_calls: dict[str, int] = field(default_factory=dict)
    session_ids: list[str] = field(default_factory=list)

    def record_call(self, tool: str, session_id: str | None) -> int:
        """Record one tool call; returns this tool's call number (1-based)."""
        self.calls_total += 1
        self.tool_calls[tool] = self.tool_calls.get(tool, 0) + 1
        sid = session_id or "<none>"
        if sid not in self.session_calls:
            self.session_ids.append(sid)
        self.session_calls[sid] = self.session_calls.get(sid, 0) + 1
        return self.tool_calls[tool]

    def snapshot(self) -> dict[str, Any]:
        return {
            "server_name": self.server_name,
            "transport": self.transport,
            "pid": os.getpid(),
            "boot_count": self.boot_count,
            "start_time": self.start_time,
            "calls_total": self.calls_total,
            "tool_calls": dict(self.tool_calls),
            "session_calls": dict(self.session_calls),
            "session_ids_seen": list(self.session_ids),
        }


def _bump_boot_count(state_dir: Path, name: str) -> int:
    """Increment and return the persistent boot counter for this server name."""
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / f"{name}.boots"
    try:
        current = int(path.read_text().strip())
    except (OSError, ValueError):
        current = 0
    current += 1
    path.write_text(f"{current}\n")
    return current


def _make_spec_tool(spec: ToolSpec, state: MockState) -> Callable[..., Any]:
    """Build an async handler whose signature matches the spec's params.

    FastMCP derives the input schema from the function signature, so we
    synthesize one: typed keyword parameters plus an injected Context.
    """

    async def handler(ctx: Context, **kwargs: Any) -> str:
        call_no = state.record_call(spec.name, ctx.session_id)
        if spec.latency_ms > 0:
            await asyncio.sleep(spec.latency_ms / 1000.0)
        fail = (
            spec.error_mode == "always"
            or (spec.error_mode == "first_n" and call_no <= spec.error_n)
            or (spec.error_mode == "after_n" and call_no > spec.error_n)
        )
        if fail:
            raise ToolError(f"{spec.name}: injected error (mode={spec.error_mode}, call={call_no})")
        try:
            return spec.response_template.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            # Bad template — fall back to a JSON dump so the call still succeeds.
            return json.dumps({"tool": spec.name, "args": kwargs})

    parameters = [
        inspect.Parameter("ctx", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Context)
    ]
    annotations: dict[str, Any] = {"ctx": Context, "return": str}
    for pname, ptype in spec.params.items():
        py_type = _PARAM_TYPES[ptype]
        parameters.append(
            inspect.Parameter(pname, inspect.Parameter.KEYWORD_ONLY, annotation=py_type)
        )
        annotations[pname] = py_type
    handler.__signature__ = inspect.Signature(parameters, return_annotation=str)  # type: ignore[attr-defined]
    handler.__annotations__ = annotations
    handler.__name__ = spec.name
    handler.__doc__ = spec.description
    return handler


def _default_specs() -> list[ToolSpec]:
    """Parity with the old test_simple_server.py: echo + add."""
    return [
        ToolSpec(
            name="echo",
            description="Echo back the message.",
            params={"message": "string"},
            response_template="Echo: {message}",
        ),
        ToolSpec(
            name="add",
            description="Add two numbers.",
            params={"a": "integer", "b": "integer"},
            # str.format can't compute; `add` is special-cased in build_server.
            response_template="Sum: {a}+{b}",
        ),
    ]


def parse_tool_specs(text: str) -> list[ToolSpec]:
    """Parse a --tools JSON document (a list, or {"tools": [...]})."""
    raw = json.loads(text)
    if isinstance(raw, dict):
        raw = raw.get("tools", [])
    if not isinstance(raw, list):
        raise ValueError("tool spec must be a JSON list or {'tools': [...]}")
    return [ToolSpec.from_dict(entry) for entry in raw]


def build_server(
    name: str,
    transport: str,
    specs: list[ToolSpec] | None = None,
    state_dir: Path | None = None,
) -> tuple[FastMCP, MockState]:
    """Assemble the mock FastMCP server plus its observable state."""
    state = MockState(server_name=name, transport=transport, state_dir=state_dir)
    if state_dir is not None:
        state.boot_count = _bump_boot_count(state_dir, name)

    mcp = FastMCP(name)

    use_defaults = specs is None
    for spec in specs if specs is not None else _default_specs():
        if use_defaults and spec.name == "add":
            # Real computed add for smoke-parity with the old simple server.
            @mcp.tool(name="add", description=spec.description)
            async def add(ctx: Context, a: int, b: int) -> str:
                state.record_call("add", ctx.session_id)
                return f"Sum: {a + b}"

            continue
        mcp.tool(_make_spec_tool(spec, state), name=spec.name)

    @mcp.tool(name="mock__whoami", description="Report mock server identity and counters.")
    async def mock__whoami(ctx: Context) -> str:
        state.record_call("mock__whoami", ctx.session_id)
        return json.dumps(state.snapshot())

    @mcp.tool(name="mock__stats", description="Report per-tool and per-session call counts.")
    async def mock__stats(ctx: Context) -> str:
        state.record_call("mock__stats", ctx.session_id)
        return json.dumps(
            {
                "tool_calls": dict(state.tool_calls),
                "session_calls": dict(state.session_calls),
                "calls_total": state.calls_total,
            }
        )

    @mcp.tool(
        name="mock__sleep",
        description="Sleep for the given number of milliseconds, then return.",
    )
    async def mock__sleep(ctx: Context, ms: int) -> str:
        state.record_call("mock__sleep", ctx.session_id)
        await asyncio.sleep(ms / 1000.0)
        return f"slept {ms}ms"

    @mcp.tool(
        name="mock__crash",
        description=(
            "Hard-exit the mock process (os._exit) after delay_ms, simulating "
            "an upstream death. Replies before exiting so the call itself succeeds."
        ),
    )
    async def mock__crash(ctx: Context, delay_ms: int = 100) -> str:
        state.record_call("mock__crash", ctx.session_id)
        loop = asyncio.get_running_loop()
        loop.call_later(delay_ms / 1000.0, os._exit, 1)
        return f"crashing in {delay_ms}ms (pid {os.getpid()})"

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        from starlette.responses import JSONResponse as _JSONResponse

        return _JSONResponse({"status": "ok", "name": state.server_name, "pid": os.getpid()})

    @mcp.custom_route("/stats", methods=["GET"])
    async def stats(request: Request) -> JSONResponse:
        from starlette.responses import JSONResponse as _JSONResponse

        return _JSONResponse(state.snapshot())

    return mcp, state


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="mcp-combiner-mockserver",
        description="Instrumentable mock MCP server (stdio or HTTP) for combiner tests",
    )
    parser.add_argument("--name", default="mock", help="Server name (default: mock)")
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default="http",
        help="Serving transport (default: http)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=9760, help="HTTP port (default: 9760)")
    parser.add_argument(
        "--tools",
        default=None,
        metavar="SPEC",
        help="Path to a tool-spec JSON file, or '-' to read the spec from stdin",
    )
    parser.add_argument(
        "--state-dir",
        default=None,
        metavar="DIR",
        help="Directory for the persistent boot counter (enables boot_count tracking)",
    )
    args = parser.parse_args(argv)

    specs: list[ToolSpec] | None = None
    if args.tools is not None:
        text = sys.stdin.read() if args.tools == "-" else Path(args.tools).read_text()
        specs = parse_tool_specs(text)

    state_dir = Path(args.state_dir) if args.state_dir else None
    mcp, _state = build_server(args.name, args.transport, specs, state_dir)

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
