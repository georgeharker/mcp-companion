"""Shared lightweight fakes for meta-tool / mount tests.

These model the FastMCP server and the connection / sharedserver managers so
diff/mount behaviour can be exercised without standing up real upstreams.
Moved here from test_reload_config.py so multiple test modules can share them.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Literal

import mcp.types as mt
from fastmcp import Client, FastMCP
from fastmcp.client.transports.http import StreamableHttpTransport
from fastmcp.client.transports.sse import SSETransport
from fastmcp.server.providers.base import Provider
from fastmcp.tools import Tool
from mcp.server.connection import Connection
from mcp.shared.dispatcher import CallOptions

from mcp_combiner.config import CombinerConfig, ServerConfig, Transport
from mcp_combiner.connections import ConnectionManager, _ManagedConnection


class ScriptedClient(Client[StreamableHttpTransport | SSETransport]):
    """A real ``Client`` (the ConnectionManager's ``HttpClient``) that never
    dials: the three methods the manager calls on a held client are scripted.

    Unscripted ``list_tools`` / ``ping`` fail loudly, so a test that didn't
    expect the manager to probe or ping finds out.
    """

    def __init__(
        self,
        *,
        connected: bool = True,
        list_tools: Callable[[], Awaitable[list[mt.Tool]]] | None = None,
        ping: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(StreamableHttpTransport("http://127.0.0.1:1/mcp"))
        self.connected = connected
        self._list_tools_script = list_tools
        self._ping_script = ping

    def is_connected(self) -> bool:
        return self.connected

    async def list_tools(
        self, max_pages: int = 250, *, cache_mode: Literal["use", "refresh", "bypass"] = "use"
    ) -> list[mt.Tool]:
        if self._list_tools_script is None:
            raise AssertionError("ScriptedClient.list_tools called but not scripted")
        return await self._list_tools_script()

    async def ping(self) -> bool:
        if self._ping_script is None:
            raise AssertionError("ScriptedClient.ping called but not scripted")
        await self._ping_script()
        return True


def tool(name: str) -> mt.Tool:
    """A minimal real ``mcp.types.Tool``."""
    return mt.Tool(name=name, input_schema={"type": "object"})


def managed_conn(
    name: str = "a",
    client: ScriptedClient | None = None,
    *,
    url: str | None = "http://127.0.0.1:1/mcp",
    shared_server: str | None = None,
    tools_ready: bool = False,
    auth_failed: bool = False,
    first_attempt_done: bool = True,
    backoff: float = 0.0,
) -> _ManagedConnection:
    """A genuine ``_ManagedConnection`` record for manager-internals tests.

    ``backoff`` defaults to 0 so ``_reconnect`` never actually sleeps;
    ``first_attempt_done`` mirrors the ``_ready`` event.
    """
    ready = asyncio.Event()
    if first_attempt_done:
        ready.set()
    return _ManagedConnection(
        name=name,
        config=CombinerConfig(),
        srv=ServerConfig(name=name, url=url, shared_server=shared_server, transport=Transport.HTTP),
        client_ref=[client],
        _tools_ready=tools_ready,
        _auth_failed=auth_failed,
        _ready=ready,
        _backoff=backoff,
    )


class FakeProvider(Provider):
    """A real fastmcp ``Provider`` serving a scripted tool list, no upstream.

    Hand it to ``_create_server_proxy`` (monkeypatched) or straight to
    ``mount_server_provider``; fastmcp's own namespace transform then applies,
    so a tool scripted as ``"ping"`` lists as ``"<server>_ping"``.
    ``on_list`` observes each underlying list call (ordering assertions).
    """

    def __init__(
        self,
        tools: Sequence[Tool] = (),
        *,
        on_list: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self._tools = list(tools)
        self._on_list = on_list

    async def _list_tools(self) -> Sequence[Tool]:
        if self._on_list is not None:
            self._on_list()
        return list(self._tools)


class ScriptedProvider(Provider):
    """A real ``Provider`` whose underlying tool listing runs *script* —
    stateful scripts model warming / failing upstreams. Exceptions propagate
    (the base ``Provider.list_tools`` does not swallow them)."""

    def __init__(self, script: Callable[[], Awaitable[Sequence[Tool]]]) -> None:
        super().__init__()
        self._script = script

    async def _list_tools(self) -> Sequence[Tool]:
        return await self._script()


class FakeCombiner(FastMCP[Any]):
    """A real ``FastMCP`` whose ``@tool()`` captures the decorated function
    instead of registering it, so tests can call meta-tools directly.

    Everything else — ``providers``, ``add_provider``, ``mount`` — is the
    real server, so mount bookkeeping is exercised for real.
    """

    def __init__(self) -> None:
        super().__init__("fake-combiner")
        self.tools: dict[str, Callable[..., Any]] = {}

    def tool(self, *args: Any, **kwargs: Any) -> Any:
        def _capture(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[fn.__name__] = fn
            return fn

        return _capture


class FakeConnManager(ConnectionManager):
    """A real ``ConnectionManager`` that never dials: connection state is a
    set of names, and every lifecycle call is recorded in ``calls``."""

    def __init__(self) -> None:
        super().__init__()
        self.connected: set[str] = set()
        self.calls: list[tuple[str, str]] = []

    def has_connection(self, name: str) -> bool:
        return name in self.connected

    def is_connected(self, name: str) -> bool:
        return name in self.connected

    def is_auth_failed(self, name: str) -> bool:
        return False

    def reset_auth_failure(self, name: str) -> None:
        self.calls.append(("reset_auth", name))

    def register(self, config: CombinerConfig, name: str, srv: ServerConfig) -> None:
        self.calls.append(("register", name))

    async def connect(self, config: CombinerConfig, name: str, srv: ServerConfig) -> None:
        self.connected.add(name)
        self.calls.append(("connect", name))

    async def disconnect(self, name: str) -> None:
        self.connected.discard(name)
        self.calls.append(("disconnect", name))


class FakeSSManager:
    def __init__(self, sharedserver_backed: set[str] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        # Names that have a backing sharedserver process (restart() returns True).
        self.sharedserver_backed = sharedserver_backed or set()

    async def ensure_started(self, server_name: str) -> None:
        self.calls.append(("start", server_name))

    async def ensure_stopped(self, server_name: str) -> None:
        self.calls.append(("stop", server_name))

    async def restart(self, server_name: str) -> bool:
        self.calls.append(("restart", server_name))
        return server_name in self.sharedserver_backed


def content_text(result: Any) -> str:
    """Join a ToolResult's text blocks — duck-typed so the mcp content union
    (TextContent/ImageContent/EmbeddedResource/...) needs no isinstance
    narrowing in tests that only want the text."""
    content = getattr(result, "content", None)
    if not isinstance(content, list):
        return ""
    return "".join(
        str(getattr(block, "text", ""))
        for block in content
        if getattr(block, "type", None) == "text"
    )


def json_object(text: str) -> dict[str, Any]:
    """Parse a tool's JSON-object text payload, failing loudly on any other
    shape (a typed boundary instead of trusting ``json.loads``'s ``Any``)."""
    value = json.loads(text)
    if not isinstance(value, dict):
        raise AssertionError(f"expected a JSON object, got {type(value).__name__}: {text!r}")
    return value


class RecordingOutbound:
    """An mcp ``Outbound`` that records the notifications a ``Connection``
    sends its client (the standalone channel a real client would read)."""

    def __init__(self) -> None:
        self.notifications: list[tuple[str, dict[str, Any]]] = []

    async def send_raw_request(
        self, method: str, params: Mapping[str, Any] | None, opts: CallOptions | None = None
    ) -> dict[str, Any]:
        raise AssertionError(f"unexpected server-to-client request: {method}")

    async def notify(
        self, method: str, params: Mapping[str, Any] | None, opts: CallOptions | None = None
    ) -> None:
        self.notifications.append((method, dict(params or {})))

    def sent(self, method: str) -> list[dict[str, Any]]:
        """The params of every *method* notification sent so far."""
        return [params for name, params in self.notifications if name == method]


def recording_connection(*, url_elicitation: bool = False) -> tuple[Connection, RecordingOutbound]:
    """A real handshake-era downstream ``Connection`` over a recording outbound."""
    outbound = RecordingOutbound()
    elicitation = (
        mt.ElicitationCapability(url=mt.UrlElicitationCapability()) if url_elicitation else None
    )
    connection = Connection(
        outbound,
        protocol_version="2025-11-25",
        client_params=mt.InitializeRequestParams(
            protocol_version="2025-11-25",
            capabilities=mt.ClientCapabilities(elicitation=elicitation),
            client_info=mt.Implementation(name="test", version="0"),
        ),
    )
    return connection, outbound
