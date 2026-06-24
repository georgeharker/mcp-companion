"""Tests for the bridge__reload_config meta-tool's diff/apply logic.

These use lightweight fakes for the FastMCP server and the connection /
sharedserver managers so the diff behaviour can be exercised without standing
up real upstream MCP servers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import mcp_bridge.server as server_mod
from mcp_bridge.config import BridgeConfig, ServerConfig
from mcp_bridge.meta_tools import register_meta_tools


class _FakeProvider:
    def __init__(self, namespace: str) -> None:
        self._namespace = namespace

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"<FakeProvider namespace='{self._namespace}'>"


class _FakeBridge:
    """Captures @bridge.tool() functions and models mount/providers."""

    def __init__(self) -> None:
        self.providers: list[Any] = []
        self.tools: dict[str, Any] = {}

    def tool(self, *_args: Any, **_kwargs: Any):
        def _decorator(fn: Any) -> Any:
            self.tools[fn.__name__] = fn
            return fn

        return _decorator

    def mount(self, proxy: Any, namespace: str) -> None:
        self.providers.append(_FakeProvider(namespace))


class _FakeConnManager:
    def __init__(self) -> None:
        self.connected: set[str] = set()
        self.calls: list[tuple[str, str]] = []

    @staticmethod
    def is_http_server(srv: ServerConfig) -> bool:
        return srv.transport.value in ("http", "sse")

    def has_connection(self, name: str) -> bool:
        return name in self.connected

    def is_auth_failed(self, name: str) -> bool:
        return False

    def reset_auth_failure(self, name: str) -> None:
        self.calls.append(("reset_auth", name))

    def register(self, _config: BridgeConfig, name: str, _srv: ServerConfig) -> None:
        self.calls.append(("register", name))

    async def connect(self, _config: BridgeConfig, name: str, _srv: ServerConfig) -> None:
        self.connected.add(name)
        self.calls.append(("connect", name))

    async def disconnect(self, name: str) -> None:
        self.connected.discard(name)
        self.calls.append(("disconnect", name))


class _FakeSSManager:
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


def _write(path: Path, servers: dict[str, Any]) -> None:
    path.write_text(json.dumps({"mcpServers": servers}))


def _http(url: str) -> dict[str, Any]:
    return {"url": url, "transport": "http"}


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg_path = tmp_path / "servers.json"
    _write(cfg_path, {"alpha": _http("http://localhost:1111/mcp")})
    config = BridgeConfig.load(str(cfg_path))

    bridge = _FakeBridge()
    conn = _FakeConnManager()
    # alpha is sharedserver-backed so restart() reports a real process bounce.
    ss = _FakeSSManager(sharedserver_backed={"alpha"})

    # _create_server_proxy / invalidate_tool_cache live in server module and are
    # imported lazily inside the tool — patch them there.
    monkeypatch.setattr(server_mod, "_create_server_proxy", lambda *a, **k: object())
    invalidated = {"count": 0}
    monkeypatch.setattr(
        server_mod,
        "invalidate_tool_cache",
        lambda: invalidated.__setitem__("count", invalidated["count"] + 1),
    )

    register_meta_tools(bridge, config, conn, ss)
    reload = bridge.tools["bridge__reload_config"]
    restart = bridge.tools["bridge__restart_server"]

    # Pretend alpha is already mounted+connected from startup.
    bridge.providers.append(_FakeProvider("alpha"))
    conn.connected.add("alpha")

    return {
        "cfg_path": cfg_path,
        "config": config,
        "bridge": bridge,
        "conn": conn,
        "ss": ss,
        "reload": reload,
        "restart": restart,
        "invalidated": invalidated,
    }


def _namespaces(bridge: _FakeBridge) -> set[str]:
    return {p._namespace for p in bridge.providers}


async def test_no_changes(harness: dict[str, Any]) -> None:
    result = await harness["reload"]()
    assert result == "No config changes detected."
    assert harness["invalidated"]["count"] == 0


async def test_add_server(harness: dict[str, Any]) -> None:
    _write(
        harness["cfg_path"],
        {"alpha": _http("http://localhost:1111/mcp"), "beta": _http("http://localhost:2222/mcp")},
    )
    result = await harness["reload"]()

    assert "mounted=['beta']" in result
    assert _namespaces(harness["bridge"]) == {"alpha", "beta"}
    assert ("connect", "beta") in harness["conn"].calls
    # alpha was untouched.
    assert ("disconnect", "alpha") not in harness["conn"].calls
    assert harness["invalidated"]["count"] == 1


async def test_remove_server(harness: dict[str, Any]) -> None:
    _write(harness["cfg_path"], {})
    result = await harness["reload"]()

    assert "removed=['alpha']" in result
    assert _namespaces(harness["bridge"]) == set()
    assert ("disconnect", "alpha") in harness["conn"].calls
    assert "alpha" not in harness["config"].servers


async def test_changed_server_remounts(harness: dict[str, Any]) -> None:
    _write(harness["cfg_path"], {"alpha": _http("http://localhost:9999/mcp")})
    result = await harness["reload"]()

    assert "changed=['alpha']" in result
    assert "mounted=['alpha']" in result
    # Unmounted then remounted exactly once each.
    assert harness["conn"].calls.count(("disconnect", "alpha")) == 1
    assert harness["conn"].calls.count(("connect", "alpha")) == 1
    assert _namespaces(harness["bridge"]) == {"alpha"}
    assert harness["config"].servers["alpha"].url == "http://localhost:9999/mcp"


async def test_disabling_server_unmounts_without_remount(harness: dict[str, Any]) -> None:
    _write(
        harness["cfg_path"],
        {"alpha": {**_http("http://localhost:1111/mcp"), "disabled": True}},
    )
    result = await harness["reload"]()

    assert "changed=['alpha']" in result
    assert "mounted=[]" in result
    assert ("disconnect", "alpha") in harness["conn"].calls
    assert _namespaces(harness["bridge"]) == set()


# --- bridge__restart_server -------------------------------------------------


async def test_restart_unknown_server(harness: dict[str, Any]) -> None:
    result = await harness["restart"]("nope")
    assert "not found" in result
    assert harness["invalidated"]["count"] == 0


async def test_restart_disabled_server_refused(harness: dict[str, Any]) -> None:
    harness["config"].servers["alpha"].disabled = True
    result = await harness["restart"]("alpha")
    assert "disabled" in result
    # Nothing torn down.
    assert ("disconnect", "alpha") not in harness["conn"].calls
    assert ("restart", "alpha") not in harness["ss"].calls


async def test_restart_sharedserver_backed(harness: dict[str, Any]) -> None:
    result = await harness["restart"]("alpha")

    assert "restarted" in result
    assert "process restarted" in result  # restart() returned True
    calls = harness["conn"].calls
    ss_calls = harness["ss"].calls
    # True restart sequence: disconnect, hard process restart, reconnect.
    assert ("disconnect", "alpha") in calls
    assert ("restart", "alpha") in ss_calls
    assert ("connect", "alpha") in calls
    # Teardown precedes reconnect (the process bounce happens in between).
    assert calls.index(("disconnect", "alpha")) < calls.index(("connect", "alpha"))
    # We never use the grace-period refcount path for a restart.
    assert ("stop", "alpha") not in ss_calls
    # Provider remounted exactly once.
    assert _namespaces(harness["bridge"]) == {"alpha"}
    assert harness["invalidated"]["count"] == 1


async def test_restart_non_sharedserver_reopens_connection(harness: dict[str, Any]) -> None:
    # beta is HTTP but NOT sharedserver-backed → restart() returns False.
    _write(
        harness["cfg_path"],
        {"alpha": _http("http://localhost:1111/mcp"), "beta": _http("http://localhost:2222/mcp")},
    )
    await harness["reload"]()  # mount beta
    harness["invalidated"]["count"] = 0
    harness["conn"].calls.clear()
    harness["ss"].calls.clear()

    result = await harness["restart"]("beta")

    assert "connection re-opened" in result  # restart() returned False
    assert ("disconnect", "beta") in harness["conn"].calls
    assert ("connect", "beta") in harness["conn"].calls
    assert "beta" in _namespaces(harness["bridge"])
