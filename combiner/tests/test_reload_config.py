"""Tests for the combiner__reload_config meta-tool's diff/apply logic.

These use lightweight fakes for the FastMCP server and the connection /
sharedserver managers so the diff behaviour can be exercised without standing
up real upstream MCP servers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeCombiner, FakeConnManager, FakeProvider, FakeSSManager

import mcp_combiner.server as server_mod
import mcp_combiner.toolcache as toolcache_mod
from mcp_combiner.config import CombinerConfig, ServerConfig
from mcp_combiner.meta_tools import register_meta_tools
from mcp_combiner.mounts import mount_server_provider


def _write(path: Path, servers: dict[str, Any]) -> None:
    path.write_text(json.dumps({"mcpServers": servers}))


def _http(url: str) -> dict[str, Any]:
    return {"url": url, "transport": "http"}


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg_path = tmp_path / "servers.json"
    _write(cfg_path, {"alpha": _http("http://localhost:1111/mcp")})
    config = CombinerConfig.load(str(cfg_path))

    combiner = FakeCombiner()
    conn = FakeConnManager()
    # alpha is sharedserver-backed so restart() reports a real process bounce.
    ss = FakeSSManager(sharedserver_backed={"alpha"})

    # _create_server_proxy / invalidate_tool_cache live in server module and are
    # imported lazily inside the tool — patch them there.
    monkeypatch.setattr(server_mod, "_create_server_proxy", lambda *a, **k: object())
    invalidated = {"count": 0}
    monkeypatch.setattr(
        server_mod,
        "invalidate_tool_cache",
        lambda: invalidated.__setitem__("count", invalidated["count"] + 1),
    )
    cleared = {"count": 0}
    monkeypatch.setattr(
        server_mod,
        "clear_tool_cache",
        lambda: cleared.__setitem__("count", cleared["count"] + 1),
    )

    register_meta_tools(combiner, config, conn, ss)
    reload = combiner.tools["combiner__reload_config"]
    restart = combiner.tools["combiner__restart_server"]

    # Pretend alpha is already mounted+connected from startup. Mount through
    # the real bookkeeping helper — startup uses the same path, and seeding
    # combiner.providers directly would bypass the mount registry (which is
    # exactly how the dead repr-matcher bug stayed invisible in these tests).
    mount_server_provider(combiner, object(), "alpha")
    conn.connected.add("alpha")

    return {
        "cfg_path": cfg_path,
        "config": config,
        "combiner": combiner,
        "conn": conn,
        "ss": ss,
        "reload": reload,
        "restart": restart,
        "invalidated": invalidated,
        "cleared": cleared,
    }


def _namespaces(combiner: FakeCombiner) -> set[str]:
    return {p._namespace for p in combiner.providers}


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
    assert _namespaces(harness["combiner"]) == {"alpha", "beta"}
    assert ("connect", "beta") in harness["conn"].calls
    # alpha was untouched.
    assert ("disconnect", "alpha") not in harness["conn"].calls
    assert harness["invalidated"]["count"] == 1


async def test_remove_server(harness: dict[str, Any]) -> None:
    _write(harness["cfg_path"], {})
    result = await harness["reload"]()

    assert "removed=['alpha']" in result
    assert _namespaces(harness["combiner"]) == set()
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
    assert _namespaces(harness["combiner"]) == {"alpha"}
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
    assert _namespaces(harness["combiner"]) == set()


# --- combiner__restart_server -------------------------------------------------


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
    assert _namespaces(harness["combiner"]) == {"alpha"}
    # HTTP: restart never proactively broadcasts. It clears the cache silently;
    # the tools/list_changed broadcast is driven by connect()'s on_tools_ready
    # (real ConnectionManager, not simulated by this fake), or the monitor's
    # re-prime once tools are genuinely listable.
    assert harness["invalidated"]["count"] == 0
    assert harness["cleared"]["count"] == 1
    assert "reconnecting" not in result  # is_connected → ready note empty


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
    assert "beta" in _namespaces(harness["combiner"])
    # HTTP restart clears silently and defers the broadcast to connect()'s
    # on_tools_ready / the monitor re-prime (not modeled by the fake conn manager).
    assert harness["invalidated"]["count"] == 0
    assert harness["cleared"]["count"] == 1


async def test_restart_http_down_defers_notification(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulate the upstream NOT coming back up synchronously: connect() runs but
    # the client never reports connected (is_connected stays False).
    conn = harness["conn"]

    async def _connect_but_down(_config: CombinerConfig, name: str, _srv: ServerConfig) -> None:
        conn.calls.append(("connect", name))
        # Deliberately do not add to conn.connected → is_connected() is False.

    monkeypatch.setattr(conn, "connect", _connect_but_down)
    harness["invalidated"]["count"] = 0
    harness["cleared"]["count"] = 0

    result = await harness["restart"]("alpha")

    # The proxy is still (re)mounted so the reconnect monitor can recover it…
    assert _namespaces(harness["combiner"]) == {"alpha"}
    # …but clients are NOT told the tools are ready while the upstream is down —
    # otherwise they'd call into a dead proxy and hang on retries. The cache is
    # cleared silently; the reconnect monitor fires list_changed once live.
    assert harness["invalidated"]["count"] == 0
    assert harness["cleared"]["count"] == 1
    assert "reconnecting" in result


async def test_restart_stdio_primes_before_broadcast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stdio/sharedserver restart must NOT broadcast until the remounted proxy
    lists tools (the started → ready gate). Restart is unified with a reconnect:
    the pre-restart slice is NOT evicted — the grace window keeps serving it until
    the fresh set is published on tools-ready."""
    import mcp_combiner.server as srv

    cfg_path = tmp_path / "servers.json"
    _write(cfg_path, {"crib": {"command": "true"}})  # stdio server (no monitor)
    config = CombinerConfig.load(str(cfg_path))

    combiner = FakeCombiner()
    conn = FakeConnManager()
    ss = FakeSSManager(sharedserver_backed={"crib"})

    order: list[str] = []

    # The prime lists tools through the MOUNTED PROVIDER (mount registry), not
    # the raw proxy — record the invocation there.
    async def _list_tools(self: Any) -> list[Any]:
        order.append("list_tools")
        return []

    monkeypatch.setattr(FakeProvider, "list_tools", _list_tools)
    monkeypatch.setattr(server_mod, "_create_server_proxy", lambda *a, **k: object())
    monkeypatch.setattr(server_mod, "invalidate_tool_cache", lambda: order.append("invalidate"))
    monkeypatch.setattr(server_mod, "clear_tool_cache", lambda: order.append("clear"))
    monkeypatch.setattr(toolcache_mod, "invalidate_tool_cache", lambda: order.append("invalidate"))
    monkeypatch.setattr(toolcache_mod, "clear_tool_cache", lambda: order.append("clear"))

    # Pre-restart: crib had a cached slice and read "ready".
    srv._server_tool_cache["crib"] = []
    srv._server_tool_seen["crib"] = 1.0
    srv._local_tools_ready["crib"] = True

    register_meta_tools(combiner, config, conn, ss)
    mount_server_provider(combiner, object(), "crib")

    try:
        result = await combiner.tools["combiner__restart_server"]("crib")
        assert "restarted" in result
        # Broadcast happens strictly AFTER the proxy lists tools — never before.
        assert order.index("list_tools") < order.index("invalidate")
        # Cache cleared silently before the ready-gated broadcast.
        assert order.index("clear") < order.index("list_tools")
        # Re-confirmed ready. Unified with reconnect: the slice is NOT evicted —
        # it is kept (grace) and refreshed by the fresh fetch after tools-ready.
        assert srv._local_tools_ready.get("crib") is True
        assert "crib" in srv._server_tool_cache
    finally:
        srv._local_tools_ready.pop("crib", None)
        srv._server_tool_cache.pop("crib", None)
        srv._server_tool_seen.pop("crib", None)
