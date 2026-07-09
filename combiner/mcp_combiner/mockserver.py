"""Instrumentable, injectable mock MCP server for exercising the combiner in tests.

Runs a FastMCP server over stdio or streamable HTTP with a configurable tool
catalog, plus always-present instrumentation and control tools that let tests:

- observe process identity (pid / persistent boot count) and per-session traffic,
- inject faults (latency, scripted errors, hard crashes),
- publish tools with **verbatim raw input schemas** — including malformed ones —
  to exercise the combiner's schema-fixing paths,
- mutate the catalog and script responses **at runtime**, in-band (mock__* tools)
  or out-of-band (HTTP /control/* routes), with tools/list_changed broadcast to
  every live session on catalog changes.

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
      "params": {"who": "string"},            # shorthand: name -> string|integer|number|boolean
      "input_schema": {...},                  # raw JSON schema, published VERBATIM
                                              # (wins over params; may be malformed)
      "output_schema": {...},                 # optional raw output schema, verbatim
      "response_template": "Hello, {who}!",   # str.format over the call args; extra
                                              # keys: {_sum} (sum of numeric args),
                                              # {_args} (JSON dump of the args)
      "responses": [                          # scripted response queue, consumed FIFO;
        {"text": "hi"},                       # falls back to response_template when empty
        {"json": {"k": "v"}},
        {"error": "boom"}
      ],
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
import base64
import contextlib
import hashlib
import itertools
import json
import os
import sys
import time
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import mcp.types
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import Tool
from fastmcp.tools.tool import ToolResult
from pydantic import PrivateAttr

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import JSONResponse, RedirectResponse
    from starlette.types import Receive, Scope, Send

_PARAM_TYPES = ("string", "integer", "number", "boolean")

_ERROR_MODES = ("none", "always", "first_n", "after_n")


@dataclass
class ToolSpec:
    """One configurable tool parsed from a spec dict (file, control tool, or route)."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] | None = None
    response_template: str = "{message}"
    responses: list[dict[str, Any]] = field(default_factory=list)
    latency_ms: float = 0.0
    error_mode: str = "none"
    error_n: int = 0

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ToolSpec:
        name = raw.get("name")
        if not name or not isinstance(name, str):
            raise ValueError(f"tool spec missing 'name': {raw!r}")

        # Schema: raw input_schema wins and is published verbatim (malformed
        # schemas are the point — they exercise the combiner's schema fixes).
        # Otherwise build a clean object schema from the params shorthand.
        if "input_schema" in raw:
            schema = raw["input_schema"]
            if not isinstance(schema, dict):
                raise ValueError(f"tool {name!r}: input_schema must be a JSON object")
        else:
            params = raw.get("params", {"message": "string"})
            for pname, ptype in params.items():
                if ptype not in _PARAM_TYPES:
                    raise ValueError(
                        f"tool {name!r}: param {pname!r} has unknown type {ptype!r} "
                        f"(expected one of {_PARAM_TYPES})"
                    )
            schema = {
                "type": "object",
                "properties": {p: {"type": t} for p, t in params.items()},
                "required": list(params),
            }

        error_mode = raw.get("error_mode", "none")
        if error_mode not in _ERROR_MODES:
            raise ValueError(
                f"tool {name!r}: unknown error_mode {error_mode!r} (expected one of {_ERROR_MODES})"
            )
        responses = raw.get("responses", [])
        if not isinstance(responses, list):
            raise ValueError(f"tool {name!r}: responses must be a list")

        first_param = next(iter(schema.get("properties", {})), "message")
        return cls(
            name=name,
            description=raw.get("description", f"Mock tool {name}"),
            input_schema=schema,
            output_schema=raw.get("output_schema"),
            response_template=raw.get("response_template", "{" + first_param + "}"),
            responses=list(responses),
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
    # Live ServerSessions, recorded per request so catalog mutations can
    # broadcast notifications/tools/list_changed (same pattern as the
    # combiner's _active_sessions WeakSet).
    sessions: weakref.WeakSet[Any] = field(default_factory=weakref.WeakSet)

    def record_call(self, tool: str, session_id: str | None) -> int:
        """Record one tool call; returns this tool's call number (1-based)."""
        self.calls_total += 1
        self.tool_calls[tool] = self.tool_calls.get(tool, 0) + 1
        sid = session_id or "<none>"
        if sid not in self.session_calls:
            self.session_ids.append(sid)
        self.session_calls[sid] = self.session_calls.get(sid, 0) + 1
        return self.tool_calls[tool]

    def record_session(self, ctx: Context) -> None:
        with contextlib.suppress(Exception):
            self.sessions.add(ctx.session)

    async def notify_tools_changed(self) -> None:
        """Broadcast tools/list_changed to every live session.

        Uses ServerSession.send_tool_list_changed() — the same call the
        combiner's _notify_tool_list_changed uses.
        """
        for session in list(self.sessions):
            with contextlib.suppress(Exception):
                await session.send_tool_list_changed()

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


class _SessionTrackingMiddleware(Middleware):
    """Record every live ServerSession so catalog mutations can broadcast
    tools/list_changed — mirrors the combiner's on_request session tracking
    (tool handlers alone never see tools/list requests)."""

    def __init__(self, state: MockState) -> None:
        self._state = state

    async def on_request(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:
        if context.fastmcp_context is not None:
            with contextlib.suppress(RuntimeError, AttributeError):
                self._state.sessions.add(context.fastmcp_context.session)
        return await call_next(context)


class MockTool(Tool):
    """A tool whose schema is published verbatim and whose behavior is scripted.

    Bypasses FastMCP's function-signature machinery entirely: `parameters` is
    the spec's raw input schema (no validation applied to it or against it),
    and `run()` consumes the response queue / template with fault injection.
    """

    _spec: ToolSpec = PrivateAttr()
    _state: MockState = PrivateAttr()

    @classmethod
    def from_spec(cls, spec: ToolSpec, state: MockState) -> MockTool:
        tool = cls(
            name=spec.name,
            description=spec.description,
            parameters=spec.input_schema,
            output_schema=spec.output_schema,
        )
        tool._spec = spec
        tool._state = state
        return tool

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        spec = self._spec
        session_id: str | None = None
        with contextlib.suppress(Exception):
            from fastmcp.server.dependencies import get_context

            ctx = get_context()
            session_id = ctx.session_id
            self._state.record_session(ctx)
        call_no = self._state.record_call(spec.name, session_id)

        if spec.latency_ms > 0:
            await asyncio.sleep(spec.latency_ms / 1000.0)

        fail = (
            spec.error_mode == "always"
            or (spec.error_mode == "first_n" and call_no <= spec.error_n)
            or (spec.error_mode == "after_n" and call_no > spec.error_n)
        )
        if fail:
            raise ToolError(f"{spec.name}: injected error (mode={spec.error_mode}, call={call_no})")

        if spec.responses:
            scripted = spec.responses.pop(0)
            if "error" in scripted:
                raise ToolError(str(scripted["error"]))
            if "json" in scripted:
                text = json.dumps(scripted["json"])
            else:
                text = str(scripted.get("text", ""))
            return ToolResult(content=[mcp.types.TextContent(type="text", text=text)])

        # Template context: the raw call args, plus computed conveniences —
        # `_sum` (sum of numeric args) and `_args` (JSON dump of the args).
        numeric = [v for v in arguments.values() if isinstance(v, (int, float))]
        template_ctx = {**arguments, "_sum": sum(numeric), "_args": json.dumps(arguments)}
        try:
            text = spec.response_template.format(**template_ctx)
        except (KeyError, IndexError, ValueError):
            # Bad template — fall back to a JSON dump so the call still succeeds.
            text = json.dumps({"tool": spec.name, "args": arguments})
        return ToolResult(content=[mcp.types.TextContent(type="text", text=text)])


def _default_specs() -> list[ToolSpec]:
    """Parity with the old test_simple_server.py: echo + add."""
    return [
        ToolSpec.from_dict(
            {
                "name": "echo",
                "description": "Echo back the message.",
                "params": {"message": "string"},
                "response_template": "Echo: {message}",
            }
        ),
        ToolSpec.from_dict(
            {
                "name": "add",
                "description": "Add two numbers.",
                "params": {"a": "integer", "b": "integer"},
                "response_template": "Sum: {_sum}",
            }
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


class MockServer:
    """The assembled mock: FastMCP instance + state + runtime catalog control."""

    def __init__(
        self,
        name: str,
        transport: str,
        specs: list[ToolSpec] | None = None,
        state_dir: Path | None = None,
        oauth: MockOAuthProvider | None = None,
    ) -> None:
        self.state = MockState(server_name=name, transport=transport, state_dir=state_dir)
        if state_dir is not None:
            self.state.boot_count = _bump_boot_count(state_dir, name)
        self.mcp = FastMCP(name)
        self.mcp.add_middleware(_SessionTrackingMiddleware(self.state))
        self._tools: dict[str, MockTool] = {}
        # Optional OAuth authorization-server capability (additive; only the
        # extra .well-known / register / authorize / token routes are added —
        # the tool catalog and its publication behaviour are untouched).
        self.oauth = oauth

        for spec in specs if specs is not None else _default_specs():
            self._add(spec)
        self._register_builtins()
        self._register_routes()
        if oauth is not None:
            oauth.register_routes(self.mcp)

    # -- catalog control ----------------------------------------------------

    def _add(self, spec: ToolSpec) -> None:
        tool = MockTool.from_spec(spec, self.state)
        if spec.name in self._tools:
            self.mcp.remove_tool(spec.name)
        self.mcp.add_tool(tool)
        self._tools[spec.name] = tool

    async def add_tool(self, spec: ToolSpec) -> None:
        self._add(spec)
        await self.state.notify_tools_changed()

    async def remove_tool(self, name: str) -> bool:
        if name not in self._tools:
            return False
        self.mcp.remove_tool(name)
        del self._tools[name]
        await self.state.notify_tools_changed()
        return True

    def push_responses(self, name: str, responses: list[dict[str, Any]]) -> None:
        self._tools[name]._spec.responses.extend(responses)

    def set_behavior(
        self,
        name: str,
        latency_ms: float | None = None,
        error_mode: str | None = None,
        error_n: int | None = None,
    ) -> None:
        spec = self._tools[name]._spec
        if latency_ms is not None:
            spec.latency_ms = latency_ms
        if error_mode is not None:
            if error_mode not in _ERROR_MODES:
                raise ValueError(f"unknown error_mode {error_mode!r}")
            spec.error_mode = error_mode
        if error_n is not None:
            spec.error_n = error_n

    # -- built-in instrumentation + control tools ---------------------------

    def _register_builtins(self) -> None:
        mcp_srv = self.mcp
        state = self.state

        @mcp_srv.tool(name="mock__whoami", description="Report mock server identity and counters.")
        async def mock__whoami(ctx: Context) -> str:
            state.record_session(ctx)
            state.record_call("mock__whoami", ctx.session_id)
            return json.dumps(state.snapshot())

        @mcp_srv.tool(
            name="mock__stats", description="Report per-tool and per-session call counts."
        )
        async def mock__stats(ctx: Context) -> str:
            state.record_session(ctx)
            state.record_call("mock__stats", ctx.session_id)
            return json.dumps(
                {
                    "tool_calls": dict(state.tool_calls),
                    "session_calls": dict(state.session_calls),
                    "calls_total": state.calls_total,
                }
            )

        @mcp_srv.tool(
            name="mock__sleep",
            description="Sleep for the given number of milliseconds, then return.",
        )
        async def mock__sleep(ctx: Context, ms: int) -> str:
            state.record_session(ctx)
            state.record_call("mock__sleep", ctx.session_id)
            await asyncio.sleep(ms / 1000.0)
            return f"slept {ms}ms"

        @mcp_srv.tool(
            name="mock__crash",
            description=(
                "Hard-exit the mock process (os._exit) after delay_ms, simulating "
                "an upstream death. Replies before exiting so the call itself succeeds."
            ),
        )
        async def mock__crash(ctx: Context, delay_ms: int = 100) -> str:
            state.record_session(ctx)
            state.record_call("mock__crash", ctx.session_id)
            loop = asyncio.get_running_loop()
            loop.call_later(delay_ms / 1000.0, os._exit, 1)
            return f"crashing in {delay_ms}ms (pid {os.getpid()})"

        @mcp_srv.tool(
            name="mock__add_tool",
            description=(
                "Add or replace a tool at runtime from a JSON spec string "
                "(same format as --tools entries; input_schema is published verbatim). "
                "Broadcasts tools/list_changed."
            ),
        )
        async def mock__add_tool(ctx: Context, spec: str) -> str:
            state.record_session(ctx)
            state.record_call("mock__add_tool", ctx.session_id)
            parsed = ToolSpec.from_dict(json.loads(spec))
            await self.add_tool(parsed)
            return f"added {parsed.name}"

        @mcp_srv.tool(
            name="mock__remove_tool",
            description="Remove a tool at runtime. Broadcasts tools/list_changed.",
        )
        async def mock__remove_tool(ctx: Context, name: str) -> str:
            state.record_session(ctx)
            state.record_call("mock__remove_tool", ctx.session_id)
            removed = await self.remove_tool(name)
            return f"removed {name}" if removed else f"no such tool {name}"

        @mcp_srv.tool(
            name="mock__push_responses",
            description=(
                "Append scripted responses (JSON list of {text}|{json}|{error} entries) "
                "to a tool's FIFO response queue."
            ),
        )
        async def mock__push_responses(ctx: Context, name: str, responses: str) -> str:
            state.record_session(ctx)
            state.record_call("mock__push_responses", ctx.session_id)
            if name not in self._tools:
                raise ToolError(f"no such tool {name}")
            entries = json.loads(responses)
            self.push_responses(name, entries)
            return f"queued {len(entries)} responses for {name}"

        @mcp_srv.tool(
            name="mock__set_behavior",
            description="Set a tool's latency_ms / error_mode / error_n at runtime.",
        )
        async def mock__set_behavior(
            ctx: Context,
            name: str,
            latency_ms: float | None = None,
            error_mode: str | None = None,
            error_n: int | None = None,
        ) -> str:
            state.record_session(ctx)
            state.record_call("mock__set_behavior", ctx.session_id)
            if name not in self._tools:
                raise ToolError(f"no such tool {name}")
            self.set_behavior(name, latency_ms, error_mode, error_n)
            return f"behavior updated for {name}"

    # -- HTTP-only out-of-band control/observation routes --------------------

    def _register_routes(self) -> None:
        mcp_srv = self.mcp
        state = self.state

        @mcp_srv.custom_route("/health", methods=["GET"])
        async def health(request: Request) -> JSONResponse:
            from starlette.responses import JSONResponse as _JSONResponse

            return _JSONResponse({"status": "ok", "name": state.server_name, "pid": os.getpid()})

        @mcp_srv.custom_route("/stats", methods=["GET"])
        async def stats(request: Request) -> JSONResponse:
            from starlette.responses import JSONResponse as _JSONResponse

            return _JSONResponse(state.snapshot())

        @mcp_srv.custom_route("/control/tools", methods=["POST"])
        async def control_add_tool(request: Request) -> JSONResponse:
            from starlette.responses import JSONResponse as _JSONResponse

            spec = ToolSpec.from_dict(await request.json())
            await self.add_tool(spec)
            return _JSONResponse({"added": spec.name})

        @mcp_srv.custom_route("/control/tools/{name}", methods=["DELETE"])
        async def control_remove_tool(request: Request) -> JSONResponse:
            from starlette.responses import JSONResponse as _JSONResponse

            name = request.path_params["name"]
            removed = await self.remove_tool(name)
            return _JSONResponse({"removed": removed}, status_code=200 if removed else 404)

        @mcp_srv.custom_route("/control/responses/{name}", methods=["POST"])
        async def control_push_responses(request: Request) -> JSONResponse:
            from starlette.responses import JSONResponse as _JSONResponse

            name = request.path_params["name"]
            if name not in self._tools:
                return _JSONResponse({"error": f"no such tool {name}"}, status_code=404)
            entries = await request.json()
            self.push_responses(name, entries)
            return _JSONResponse({"queued": len(entries)})

        @mcp_srv.custom_route("/control/behavior/{name}", methods=["POST"])
        async def control_set_behavior(request: Request) -> JSONResponse:
            from starlette.responses import JSONResponse as _JSONResponse

            name = request.path_params["name"]
            if name not in self._tools:
                return _JSONResponse({"error": f"no such tool {name}"}, status_code=404)
            body = await request.json()
            self.set_behavior(
                name,
                body.get("latency_ms"),
                body.get("error_mode"),
                body.get("error_n"),
            )
            return _JSONResponse({"ok": True})


# ── OAuth authorization-server capability (additive) ──────────────────
#
# Layers a minimal, self-validating OAuth 2.1 authorization server onto the
# mock so tests can exercise the combiner's *real* _RefreshTokenOAuth
# end-to-end and assert the exact access / refresh tokens given to, and
# received back from, the upstream MCP server.  None of this touches the tool
# catalog or its publication behaviour — it only adds .well-known / register /
# authorize / token HTTP routes plus a bearer guard on /mcp.


@dataclass
class MockOAuthConfig:
    """Knobs controlling how the mock provider issues and validates tokens."""

    issue_refresh_token: bool = True
    """Include a ``refresh_token`` in the initial authorization_code response."""

    rotate_refresh_token: bool = True
    """Mint a *new* refresh_token on each refresh (vs. re-issuing the same one)."""

    include_refresh_on_refresh: bool = True
    """Include a ``refresh_token`` field at all in refresh responses.

    ``False`` mimics Google, which omits it — the combiner must preserve the
    original.  When ``False`` it overrides :attr:`rotate_refresh_token`.
    """

    access_token_ttl: int = 3600
    """``expires_in`` (seconds) reported for every minted access token."""

    verify_pkce: bool = True
    """Verify the S256 ``code_verifier`` against the stored ``code_challenge``."""

    require_bearer: bool = True
    """401 ``/mcp`` requests that lack a currently-valid access token."""

    mcp_path: str = "/mcp"
    """Path prefix the bearer guard protects."""


@dataclass
class TokenRequest:
    """One request the mock's token endpoint received."""

    grant_type: str
    client_id: str | None
    code: str | None
    refresh_token: str | None
    resource: str | None
    form: dict[str, str]


@dataclass
class IssuedTokens:
    """One (access, refresh) pair the mock minted, and the grant that caused it."""

    access_token: str
    refresh_token: str | None
    grant_type: str


@dataclass
class MockOAuthAudit:
    """Everything the provider issued / received, for test assertions."""

    registrations: list[dict[str, Any]] = field(default_factory=list)
    authorize_requests: list[dict[str, str]] = field(default_factory=list)
    token_requests: list[TokenRequest] = field(default_factory=list)
    issued: list[IssuedTokens] = field(default_factory=list)
    # Access token presented on each protected /mcp request (None if absent).
    bearer_seen: list[str | None] = field(default_factory=list)
    pkce_failures: int = 0

    def token_requests_of(self, grant_type: str) -> list[TokenRequest]:
        return [r for r in self.token_requests if r.grant_type == grant_type]

    def authenticated_mcp_calls(self) -> list[str]:
        """Access tokens that actually reached the protected endpoint."""
        return [t for t in self.bearer_seen if t is not None]


def _s256_challenge(code_verifier: str) -> str:
    """Compute the S256 PKCE challenge for a verifier (base64url, no padding)."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _bearer_from_scope(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            decoded = bytes(value).decode("latin-1")
            if decoded.lower().startswith("bearer "):
                return decoded[7:]
            return None
    return None


class MockOAuthProvider:
    """A minimal, self-validating OAuth 2.1 authorization server for tests.

    Pass an instance to :class:`MockServer` (or :func:`build_server`) to attach
    its routes, then wrap the resulting ASGI app with :meth:`guard` to enforce
    bearer auth on ``/mcp``.  The AS advertises *itself* as the authorization
    server (ClickUp-style), so the combiner treats its 401 as authoritative.
    """

    def __init__(
        self,
        *,
        resource_url: str,
        issuer_url: str,
        config: MockOAuthConfig | None = None,
    ) -> None:
        """``resource_url`` is the MCP endpoint (``http://h:p/mcp``); ``issuer_url``
        is the origin serving the OAuth endpoints (``http://h:p``)."""
        self.config = config or MockOAuthConfig()
        self.audit = MockOAuthAudit()
        self.resource_url = resource_url.rstrip("/")
        self.issuer_url = issuer_url.rstrip("/")

        self.valid_access_tokens: set[str] = set()
        self.valid_refresh_tokens: set[str] = set()
        self.pending_codes: dict[str, dict[str, str]] = {}
        self.registered_clients: dict[str, dict[str, Any]] = {}

        self._client_counter = itertools.count(1)
        self._access_counter = itertools.count(1)
        self._refresh_counter = itertools.count(1)
        self._code_counter = itertools.count(1)

    # -- minting --------------------------------------------------------

    def seed_refresh_token(self, token: str) -> None:
        """Register *token* as a valid refresh token (for refresh-only tests)."""
        self.valid_refresh_tokens.add(token)

    def _mint_access(self) -> str:
        tok = f"at-{next(self._access_counter)}"
        self.valid_access_tokens.add(tok)
        return tok

    def _mint_refresh(self) -> str:
        tok = f"rt-{next(self._refresh_counter)}"
        self.valid_refresh_tokens.add(tok)
        return tok

    def _token_payload(self, grant_type: str, *, issue_refresh: bool) -> dict[str, Any]:
        access = self._mint_access()
        refresh: str | None = self._mint_refresh() if issue_refresh else None
        self.audit.issued.append(
            IssuedTokens(access_token=access, refresh_token=refresh, grant_type=grant_type)
        )
        payload: dict[str, Any] = {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": self.config.access_token_ttl,
            "scope": "mock.read mock.write",
        }
        if refresh is not None:
            payload["refresh_token"] = refresh
        return payload

    # -- route registration --------------------------------------------

    def register_routes(self, mcp: FastMCP) -> None:
        """Attach the discovery / register / authorize / token routes to *mcp*."""
        prm_body = {
            "resource": self.resource_url,
            "authorization_servers": [self.issuer_url],
        }
        as_body = {
            "issuer": self.issuer_url,
            "authorization_endpoint": f"{self.issuer_url}/oauth/authorize",
            "token_endpoint": f"{self.issuer_url}/oauth/token",
            "registration_endpoint": f"{self.issuer_url}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
            "scopes_supported": ["mock.read", "mock.write"],
        }

        # RFC 9728 discovery — the SDK probes both the path-suffixed and the
        # bare well-known URLs; register both so discovery succeeds regardless.
        async def prm(request: Request) -> JSONResponse:
            from starlette.responses import JSONResponse as _JSONResponse

            return _JSONResponse(prm_body)

        async def asm(request: Request) -> JSONResponse:
            from starlette.responses import JSONResponse as _JSONResponse

            return _JSONResponse(as_body)

        mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])(prm)
        mcp.custom_route("/.well-known/oauth-protected-resource/mcp", methods=["GET"])(prm)
        mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])(asm)
        mcp.custom_route("/.well-known/openid-configuration", methods=["GET"])(asm)

        @mcp.custom_route("/oauth/register", methods=["POST"])
        async def register(request: Request) -> JSONResponse:
            from starlette.responses import JSONResponse as _JSONResponse

            body = await request.json()
            client_id = f"client-{next(self._client_counter)}"
            record: dict[str, Any] = {
                "client_id": client_id,
                "client_id_issued_at": int(time.time()),
                "redirect_uris": body.get("redirect_uris", []),
                "grant_types": body.get("grant_types", ["authorization_code", "refresh_token"]),
                "response_types": body.get("response_types", ["code"]),
                "token_endpoint_auth_method": body.get("token_endpoint_auth_method", "none"),
                "scope": body.get("scope", "mock.read mock.write"),
            }
            self.registered_clients[client_id] = record
            self.audit.registrations.append({"request": body, "client_id": client_id})
            return _JSONResponse(record, status_code=201)

        @mcp.custom_route("/oauth/authorize", methods=["GET"])
        async def authorize(request: Request) -> RedirectResponse | JSONResponse:
            from starlette.responses import JSONResponse as _JSONResponse
            from starlette.responses import RedirectResponse as _RedirectResponse

            params = {k: str(v) for k, v in request.query_params.items()}
            self.audit.authorize_requests.append(params)

            redirect_uri = params.get("redirect_uri")
            if not redirect_uri:
                return _JSONResponse({"error": "invalid_request"}, status_code=400)

            code = f"code-{next(self._code_counter)}"
            self.pending_codes[code] = {
                "client_id": params.get("client_id", ""),
                "code_challenge": params.get("code_challenge", ""),
                "redirect_uri": redirect_uri,
            }
            sep = "&" if "?" in redirect_uri else "?"
            location = f"{redirect_uri}{sep}code={code}"
            if state := params.get("state"):
                location += f"&state={state}"
            return _RedirectResponse(location, status_code=302)

        @mcp.custom_route("/oauth/token", methods=["POST"])
        async def token(request: Request) -> JSONResponse:
            form = {k: str(v) for k, v in (await request.form()).items()}
            grant_type = form.get("grant_type", "")
            self.audit.token_requests.append(
                TokenRequest(
                    grant_type=grant_type,
                    client_id=form.get("client_id"),
                    code=form.get("code"),
                    refresh_token=form.get("refresh_token"),
                    resource=form.get("resource"),
                    form=form,
                )
            )
            if grant_type == "authorization_code":
                return self._grant_authorization_code(form)
            if grant_type == "refresh_token":
                return self._grant_refresh_token(form)
            from starlette.responses import JSONResponse as _JSONResponse

            return _JSONResponse(
                {"error": "unsupported_grant_type", "grant_type": grant_type}, status_code=400
            )

    # -- grant handlers -------------------------------------------------

    def _grant_authorization_code(self, form: dict[str, str]) -> JSONResponse:
        from starlette.responses import JSONResponse as _JSONResponse

        code = form.get("code", "")
        pending = self.pending_codes.pop(code, None)
        if pending is None:
            return _JSONResponse({"error": "invalid_grant"}, status_code=400)

        if self.config.verify_pkce and pending.get("code_challenge"):
            verifier = form.get("code_verifier", "")
            if _s256_challenge(verifier) != pending["code_challenge"]:
                self.audit.pkce_failures += 1
                return _JSONResponse(
                    {"error": "invalid_grant", "detail": "PKCE mismatch"}, status_code=400
                )

        payload = self._token_payload(
            "authorization_code", issue_refresh=self.config.issue_refresh_token
        )
        return _JSONResponse(payload)

    def _grant_refresh_token(self, form: dict[str, str]) -> JSONResponse:
        from starlette.responses import JSONResponse as _JSONResponse

        presented = form.get("refresh_token", "")
        if presented not in self.valid_refresh_tokens:
            return _JSONResponse(
                {"error": "invalid_grant", "detail": "unknown refresh_token"}, status_code=400
            )

        include_refresh = self.config.include_refresh_on_refresh
        if include_refresh and not self.config.rotate_refresh_token:
            # Re-issue the *same* refresh token: mint access only, echo old RT.
            payload = self._token_payload("refresh_token", issue_refresh=False)
            payload["refresh_token"] = presented
            self.audit.issued[-1].refresh_token = presented
        else:
            payload = self._token_payload("refresh_token", issue_refresh=include_refresh)
            if include_refresh:
                # A rotated refresh token invalidates the presented one.
                self.valid_refresh_tokens.discard(presented)
        return _JSONResponse(payload)

    # -- bearer enforcement --------------------------------------------

    def guard(self, app: Any) -> Any:
        """Wrap an ASGI *app* so protected ``/mcp`` requests need a valid bearer."""

        async def wrapped(scope: Scope, receive: Receive, send: Send) -> None:
            if (
                scope["type"] == "http"
                and self.config.require_bearer
                and self._is_protected(scope.get("path", ""))
            ):
                token = _bearer_from_scope(scope)
                self.audit.bearer_seen.append(token)
                if token is None or token not in self.valid_access_tokens:
                    await self._send_401(send)
                    return
            await app(scope, receive, send)

        return wrapped

    def _is_protected(self, path: str) -> bool:
        p = self.config.mcp_path
        return path == p or path.startswith(p + "/")

    async def _send_401(self, send: Send) -> None:
        www_auth = (
            f'Bearer resource_metadata='
            f'"{self.issuer_url}/.well-known/oauth-protected-resource"'
        )
        body = json.dumps({"error": "invalid_token"}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", www_auth.encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def build_server(
    name: str,
    transport: str,
    specs: list[ToolSpec] | None = None,
    state_dir: Path | None = None,
    oauth: MockOAuthProvider | None = None,
) -> tuple[FastMCP, MockState]:
    """Assemble the mock server; returns (FastMCP instance, observable state)."""
    server = MockServer(name, transport, specs, state_dir, oauth)
    return server.mcp, server.state


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
    parser.add_argument(
        "--oauth",
        action="store_true",
        help=(
            "Serve as a self-validating OAuth 2.1 authorization server: adds "
            ".well-known/register/authorize/token routes and requires a valid "
            "bearer on /mcp (HTTP transport only)."
        ),
    )
    args = parser.parse_args(argv)

    specs: list[ToolSpec] | None = None
    if args.tools is not None:
        text = sys.stdin.read() if args.tools == "-" else Path(args.tools).read_text()
        specs = parse_tool_specs(text)

    state_dir = Path(args.state_dir) if args.state_dir else None

    if args.oauth:
        if args.transport != "http":
            parser.error("--oauth requires --transport http")
        import uvicorn

        issuer = f"http://{args.host}:{args.port}"
        provider = MockOAuthProvider(
            resource_url=f"{issuer}/mcp", issuer_url=issuer
        )
        mcp_srv, _state = build_server(args.name, args.transport, specs, state_dir, provider)
        app = provider.guard(mcp_srv.http_app())
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
        return

    mcp_srv, _state = build_server(args.name, args.transport, specs, state_dir)

    if args.transport == "stdio":
        mcp_srv.run(transport="stdio")
    else:
        mcp_srv.run(transport="http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
