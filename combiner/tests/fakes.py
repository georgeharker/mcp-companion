"""Shared lightweight fakes for meta-tool / mount tests.

These model the FastMCP server and the connection / sharedserver managers so
diff/mount behaviour can be exercised without standing up real upstreams.
Moved here from test_reload_config.py so multiple test modules can share them.
"""

from __future__ import annotations

from typing import Any

from mcp_combiner.config import CombinerConfig, ServerConfig


class FakeProvider:
    def __init__(self, namespace: str) -> None:
        self._namespace = namespace

    async def list_tools(self) -> list[Any]:
        return []

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"<FakeProvider namespace='{self._namespace}'>"


class FakeCombiner:
    """Captures @combiner.tool() functions and models mount/providers."""

    def __init__(self) -> None:
        self.providers: list[Any] = []
        self.tools: dict[str, Any] = {}

    def tool(self, *_args: Any, **_kwargs: Any):
        def _decorator(fn: Any) -> Any:
            self.tools[fn.__name__] = fn
            return fn

        return _decorator

    def mount(self, proxy: Any, namespace: str) -> None:
        self.providers.append(FakeProvider(namespace))


class FakeConnManager:
    def __init__(self) -> None:
        self.connected: set[str] = set()
        self.calls: list[tuple[str, str]] = []

    @staticmethod
    def is_http_server(srv: ServerConfig) -> bool:
        return srv.transport.value in ("http", "sse")

    def has_connection(self, name: str) -> bool:
        return name in self.connected

    def is_connected(self, name: str) -> bool:
        return name in self.connected

    def is_auth_failed(self, name: str) -> bool:
        return False

    def reset_auth_failure(self, name: str) -> None:
        self.calls.append(("reset_auth", name))

    def register(self, _config: CombinerConfig, name: str, _srv: ServerConfig) -> None:
        self.calls.append(("register", name))

    async def connect(self, _config: CombinerConfig, name: str, _srv: ServerConfig) -> None:
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

    async def ensure_started(self, name: str) -> None:
        self.calls.append(("start", name))

    async def ensure_stopped(self, name: str) -> None:
        self.calls.append(("stop", name))

    async def restart(self, name: str) -> bool:
        self.calls.append(("restart", name))
        return name in self.sharedserver_backed
