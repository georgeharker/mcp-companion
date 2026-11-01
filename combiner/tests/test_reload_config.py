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
from fastmcp.tools.function_tool import FunctionTool

import mcp_combiner.proxyfactory as proxyfactory_mod
import mcp_combiner.server as server_mod
import mcp_combiner.toolcache as toolcache_mod
from mcp_combiner.config import CombinerConfig, ServerConfig
from mcp_combiner.meta_tools import register_meta_tools
from mcp_combiner.mounts import mount_server_provider, mounted_server_names


def _write(path: Path, servers: dict[str, Any]) -> None:
    path.write_text(json.dumps({"mcpServers": servers}))


def _http(url: str) -> dict[str, Any]:
    return {"url": url, "transport": "http"}


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    cfg_path = tmp_path / "servers.json"
    _write(cfg_path, {"alpha": _http("http://localhost:1111/mcp")})
    config = CombinerConfig.load(str(cfg_path))

    combiner = FakeCombiner()
    conn = FakeConnManager()
    # alpha is sharedserver-backed so restart() reports a real process bounce.
    ss = FakeSSManager(sharedserver_backed={"alpha"})

    # _create_server_proxy / invalidate_tool_cache are imported lazily inside
    # the meta-tools from their owning modules — patch them there.
    monkeypatch.setattr(proxyfactory_mod, "_create_server_proxy", lambda *a, **k: object())
    invalidated = {"count": 0}
    monkeypatch.setattr(
        toolcache_mod,
        "invalidate_tool_cache",
        lambda: invalidated.__setitem__("count", invalidated["count"] + 1),
    )
    cleared = {"count": 0}
    monkeypatch.setattr(
        toolcache_mod,
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
    return mounted_server_names(combiner)


def _ping_tool() -> FunctionTool:
    """A real tool the scripted provider serves as ``ping`` (fastmcp's
    namespace transform lists it as ``<server>_ping``)."""
    return FunctionTool(
        fn=lambda: None,
        name="ping",
        description="",
        parameters={"type": "object", "properties": {}},
    )


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
    # the raw proxy — record the invocation there. Answer a REAL tool: the
    # publication is gated on a non-empty list (and this one differs from the
    # pre-restart slice, so it is a replacement → broadcast).
    remounted = FakeProvider([_ping_tool()], on_list=lambda: order.append("list_tools"))
    monkeypatch.setattr(proxyfactory_mod, "_create_server_proxy", lambda *a, **k: remounted)
    monkeypatch.setattr(server_mod, "invalidate_tool_cache", lambda: order.append("invalidate"))
    monkeypatch.setattr(server_mod, "clear_tool_cache", lambda: order.append("clear"))
    monkeypatch.setattr(toolcache_mod, "invalidate_tool_cache", lambda: order.append("invalidate"))
    monkeypatch.setattr(toolcache_mod, "clear_tool_cache", lambda: order.append("clear"))

    # Pre-restart: crib had a confirmed (empty-after-filter) slice → "ready".
    srv._server_tool_cache["crib"] = []
    srv._server_tool_seen["crib"] = 1.0

    register_meta_tools(combiner, config, conn, ss)
    mount_server_provider(combiner, object(), "crib")

    try:
        result = await combiner.tools["combiner__restart_server"]("crib")
        assert "restarted" in result
        # Broadcast happens strictly AFTER the proxy lists tools — never before.
        assert order.index("list_tools") < order.index("invalidate")
        # Cache cleared silently before the ready-gated broadcast.
        assert order.index("clear") < order.index("list_tools")
        # Re-confirmed. Unified with reconnect: the slice is NOT evicted — it
        # is kept (grace) and replaced by the fresh list once tools answer.
        assert "crib" in srv._server_tool_cache
    finally:
        srv._server_tool_cache.pop("crib", None)
        srv._server_tool_seen.pop("crib", None)


async def test_restart_stdio_empty_answer_keeps_slice_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restarted server that only answers EMPTY tools/list (respawned
    process, registry not yet populated) must neither broadcast nor clobber
    the last-known-good confirmed slice — hysteresis keeps serving it until a
    real list arrives (prime retry, monitor re-probe, or merge promotion
    publishes then)."""
    import mcp_combiner.server as srv

    cfg_path = tmp_path / "servers.json"
    _write(cfg_path, {"crib": {"command": "true"}})
    config = CombinerConfig.load(str(cfg_path))

    combiner = FakeCombiner()
    conn = FakeConnManager()
    ss = FakeSSManager(sharedserver_backed={"crib"})

    order: list[str] = []

    remounted = FakeProvider([], on_list=lambda: order.append("list_tools"))
    monkeypatch.setattr(proxyfactory_mod, "_create_server_proxy", lambda *a, **k: remounted)
    monkeypatch.setattr(server_mod, "invalidate_tool_cache", lambda: order.append("invalidate"))
    monkeypatch.setattr(server_mod, "clear_tool_cache", lambda: order.append("clear"))
    monkeypatch.setattr(toolcache_mod, "invalidate_tool_cache", lambda: order.append("invalidate"))
    monkeypatch.setattr(toolcache_mod, "clear_tool_cache", lambda: order.append("clear"))

    # Keep the empty-answer retry window short — the real prime retries an
    # answered-empty list until its (default 30s) deadline.
    real_prime = toolcache_mod.prime_server_tools

    async def fast_prime(
        combiner_: Any, name_: Any, timeout: float | None = None, interval: float | None = None
    ) -> Any:
        return await real_prime(combiner_, name_, timeout=0.05, interval=0.0)

    monkeypatch.setattr(toolcache_mod, "prime_server_tools", fast_prime)

    good = FunctionTool(
        fn=lambda: None,
        name="crib_ping",
        description="",
        parameters={"type": "object", "properties": {}},
    )
    srv._server_tool_cache["crib"] = [good]
    srv._server_tool_seen["crib"] = 1.0

    register_meta_tools(combiner, config, conn, ss)
    mount_server_provider(combiner, object(), "crib")

    try:
        result = await combiner.tools["combiner__restart_server"]("crib")
        assert "restarted" in result
        assert "no tools yet" in result  # the EMPTY outcome is surfaced
        # An answered-but-empty list is never a publication.
        assert "invalidate" not in order
        # The last-known-good confirmed slice survives the empty answers.
        assert [str(t.name) for t in srv._server_tool_cache["crib"]] == ["crib_ping"]
    finally:
        srv._server_tool_cache.pop("crib", None)
        srv._server_tool_seen.pop("crib", None)


async def test_reload_added_stdio_primes_before_broadcast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reloading in a NEW stdio/sharedserver server must prime it (started →
    ready) so its tools become listable, exactly as combiner__enable_server does.

    Without the prime, reload mounts the server but broadcasts
    tools/list_changed before the upstream can answer tools/list — clients
    re-fetch and the new tools are absent, with no later notification once the
    server primes. (HTTP-added servers escape this because _mount_server's
    connect() fires on_tools_ready; stdio/sharedserver ones do not.)"""
    import mcp_combiner.server as srv

    cfg_path = tmp_path / "servers.json"
    # An HTTP server that stays byte-identical across the reload, so only the
    # added stdio server is touched.
    _write(cfg_path, {"alpha": _http("http://localhost:1111/mcp")})
    config = CombinerConfig.load(str(cfg_path))

    combiner = FakeCombiner()
    conn = FakeConnManager()
    ss = FakeSSManager(sharedserver_backed={"crib"})

    order: list[str] = []

    added = FakeProvider([_ping_tool()], on_list=lambda: order.append("list_tools"))
    monkeypatch.setattr(proxyfactory_mod, "_create_server_proxy", lambda *a, **k: added)
    monkeypatch.setattr(server_mod, "invalidate_tool_cache", lambda: order.append("invalidate"))
    monkeypatch.setattr(server_mod, "clear_tool_cache", lambda: order.append("clear"))
    monkeypatch.setattr(toolcache_mod, "invalidate_tool_cache", lambda: order.append("invalidate"))
    monkeypatch.setattr(toolcache_mod, "clear_tool_cache", lambda: order.append("clear"))

    register_meta_tools(combiner, config, conn, ss)
    # alpha mounted+connected from startup; crib is not present yet.
    mount_server_provider(combiner, object(), "alpha")
    conn.connected.add("alpha")

    # Add a stdio server to the on-disk config and reload it in.
    _write(
        cfg_path,
        {"alpha": _http("http://localhost:1111/mcp"), "crib": {"command": "true"}},
    )

    try:
        result = await combiner.tools["combiner__reload_config"]()
        assert "mounted=['crib']" in result
        # The added stdio server was primed — its provider listed tools. This is
        # exactly what the pre-fix reload skipped (list_tools never ran).
        assert "list_tools" in order
        # Primed strictly BEFORE any broadcast, never after.
        assert order.index("list_tools") < order.index("invalidate")
        # Its tools are now confirmed and listable.
        assert "crib" in srv._server_tool_cache
        assert [str(t.name) for t in srv._server_tool_cache["crib"]] == ["crib_ping"]
    finally:
        srv._server_tool_cache.pop("crib", None)
        srv._server_tool_seen.pop("crib", None)


async def test_reload_picks_up_external_backing_rewrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: an edit confined to `externalServers` (launch/stop argv)
    must NOT be reported as "No config changes detected" — backing-entry
    diffs count as server changes, so the referencing server is torn down
    (stop via the entry snapshot) and remounted (relaunched with the new
    argv)."""
    cfg_path = tmp_path / "servers.json"
    base: dict[str, Any] = {
        "mcpServers": {
            "beta": {
                "url": "http://localhost:2222/mcp",
                "transport": "http",
                "externalServer": "ext",
            },
        },
        "externalServers": {
            "ext": {
                "launch": ["docker", "run", "-d", "x"],
                "stop": ["docker", "stop", "OLD"],
                "health_timeout": 5,
            },
        },
    }
    cfg_path.write_text(json.dumps(base))
    config = CombinerConfig.load(str(cfg_path))

    combiner = FakeCombiner()
    conn = FakeConnManager()
    ss = FakeSSManager()
    monkeypatch.setattr(proxyfactory_mod, "_create_server_proxy", lambda *a, **k: object())
    monkeypatch.setattr(toolcache_mod, "invalidate_tool_cache", lambda: None)
    monkeypatch.setattr(toolcache_mod, "clear_tool_cache", lambda: None)

    register_meta_tools(combiner, config, conn, ss)
    reload = combiner.tools["combiner__reload_config"]
    mount_server_provider(combiner, object(), "beta")
    conn.connected.add("beta")

    # Reload with ONLY the externalServers entry changed (new stop argv).
    rewrite = json.loads(json.dumps(base))
    rewrite["externalServers"]["ext"]["stop"] = ["docker", "stop", "NEW"]
    cfg_path.write_text(json.dumps(rewrite))

    result = await reload()
    assert "changed=['beta']" in result
    assert config.external_servers["ext"].stop == ["docker", "stop", "NEW"]
    # Teardown stopped the backend, remount re-started it (fake manager bookkeeping).
    assert ("stop", "beta") in ss.calls
    assert ("start", "beta") in ss.calls


async def test_reload_removing_server_and_entry_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: deleting a server AND its externalServers entry in the same
    edit used to KeyError mid-reload (the backing-diff scan included removed
    servers in `changed`, but the remount step indexes new_servers). The
    removal path alone must handle it."""
    cfg_path = tmp_path / "servers.json"
    base: dict[str, Any] = {
        "mcpServers": {
            "alpha": {"url": "http://localhost:1111/mcp", "transport": "http"},
            "beta": {
                "url": "http://localhost:2222/mcp",
                "transport": "http",
                "externalServer": "ext",
            },
        },
        "externalServers": {
            "ext": {
                "launch": ["docker", "run", "-d", "x"],
                "stop": ["docker", "stop", "OLD"],
                "health_timeout": 5,
            },
        },
    }
    cfg_path.write_text(json.dumps(base))
    config = CombinerConfig.load(str(cfg_path))

    combiner = FakeCombiner()
    conn = FakeConnManager()
    ss = FakeSSManager()
    monkeypatch.setattr(proxyfactory_mod, "_create_server_proxy", lambda *a, **k: object())
    monkeypatch.setattr(toolcache_mod, "invalidate_tool_cache", lambda: None)
    monkeypatch.setattr(toolcache_mod, "clear_tool_cache", lambda: None)

    register_meta_tools(combiner, config, conn, ss)
    reload = combiner.tools["combiner__reload_config"]
    mount_server_provider(combiner, object(), "alpha")
    mount_server_provider(combiner, object(), "beta")
    conn.connected.update({"alpha", "beta"})

    # Remove beta AND its backing entry in one edit.
    cfg_path.write_text(json.dumps({"mcpServers": {"alpha": base["mcpServers"]["alpha"]}}))

    result = await reload()
    assert "removed=['beta']" in result
    assert "changed" not in result.replace("changed=[]", "")  # beta not misreported as changed
    assert "beta" not in config.servers
    assert ("disconnect", "beta") in conn.calls


async def test_disable_evicts_isolated_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (round-6 B-1): disable must evict per-chat isolated
    sessions/processes — the round-5 fix covered reload/restart teardowns but
    disable has its own inline teardown, and re-enable handed chats their OLD
    children (same PID, stale state) until this landed."""
    cfg_path = tmp_path / "servers.json"
    _write(cfg_path, {"alpha": _http("http://localhost:1111/mcp")})
    config = CombinerConfig.load(str(cfg_path))

    combiner = FakeCombiner()
    conn = FakeConnManager()
    ss = FakeSSManager()

    import mcp_combiner.isolated as iso

    called: list[str] = []

    async def _spy(server: str) -> int:
        called.append(server)
        return 2

    monkeypatch.setattr(iso.REGISTRY, "evict_server", _spy)

    register_meta_tools(combiner, config, conn, ss)
    mount_server_provider(combiner, object(), "alpha")

    result = await combiner.tools["combiner__disable_server"]("alpha")
    assert "disabled and unmounted" in result
    assert called == ["alpha"]


async def test_disable_without_evict_still_fires_and_reports_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (round-9): when provider dropping fails, the evict must
    STILL run (it lives in the finally now — pre-fix it lived inside the try
    and a drop exception skipped it, leaving per-chat children alive behind a
    disabled server), and the failure message is preserved."""
    cfg_path = tmp_path / "servers.json"
    _write(cfg_path, {"alpha": _http("http://localhost:1111/mcp")})
    config = CombinerConfig.load(str(cfg_path))

    combiner = FakeCombiner()
    conn = FakeConnManager()
    ss = FakeSSManager()

    import mcp_combiner.isolated as iso

    called: list[str] = []

    async def _spy(server: str) -> int:
        called.append(server)
        return 1

    monkeypatch.setattr(iso.REGISTRY, "evict_server", _spy)

    def _explode(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("kaboom")

    monkeypatch.setattr("mcp_combiner.meta_tools.drop_server_providers", _explode)

    register_meta_tools(combiner, config, conn, ss)
    mount_server_provider(combiner, object(), "alpha")

    result = await combiner.tools["combiner__disable_server"]("alpha")
    assert "disabled but failed to unmount: kaboom" in result
    assert called == ["alpha"]  # the evict survived the drop failure


async def test_disable_drops_providers_before_it_evicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Order pin (round-9): disable tears the mount down BEFORE evicting —
    a call still in flight through the old proxy cannot re-register a stale
    entry after the evict."""
    cfg_path = tmp_path / "servers.json"
    _write(cfg_path, {"alpha": _http("http://localhost:1111/mcp")})
    config = CombinerConfig.load(str(cfg_path))

    combiner = FakeCombiner()
    conn = FakeConnManager()
    ss = FakeSSManager()

    import mcp_combiner.isolated as iso

    order: list[str] = []

    async def _evict_spy(server: str) -> int:
        order.append("evict")
        return 1

    def _drop_spy(*args: Any, **kwargs: Any) -> int:
        order.append("drop")
        return 1

    monkeypatch.setattr(iso.REGISTRY, "evict_server", _evict_spy)
    monkeypatch.setattr("mcp_combiner.meta_tools.drop_server_providers", _drop_spy)

    register_meta_tools(combiner, config, conn, ss)
    mount_server_provider(combiner, object(), "alpha")

    await combiner.tools["combiner__disable_server"]("alpha")
    assert order == ["drop", "evict"]
