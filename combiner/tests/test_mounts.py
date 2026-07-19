"""Regression tests for explicit mount bookkeeping (COMBINER-RESTART-BUG.md).

The old unmount matcher repr-sniffed fastmcp's provider wrappers
(``namespace='name'`` / a ``_namespace`` attribute) and silently matched
nothing on fastmcp 3.x — restart/disable left stale providers mounted, and
every proxied call then failed against a dead connection closure. These tests
run against the REAL fastmcp mount path, which the fake-based tests can never
regression-cover.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeConnManager, FakeSSManager
from fastmcp import Client, FastMCP

import mcp_combiner.proxyfactory as proxyfactory_mod
import mcp_combiner.server as server_mod
import mcp_combiner.toolcache as toolcache_mod
from mcp_combiner.config import CombinerConfig
from mcp_combiner.meta_tools import register_meta_tools
from mcp_combiner.mounts import drop_server_providers, mount_server_provider


def _child(name: str = "child") -> FastMCP:
    srv = FastMCP(name)

    @srv.tool()
    def ping() -> str:
        return "pong"

    return srv


def test_drop_removes_real_mount() -> None:
    """The regression test that would have caught the fastmcp repr drift."""
    combiner = FastMCP("parent")
    base = len(combiner.providers)

    mount_server_provider(combiner, _child(), "crib")
    assert len(combiner.providers) == base + 1

    assert drop_server_providers(combiner, "crib") == 1
    assert len(combiner.providers) == base
    # Idempotent: nothing left to drop.
    assert drop_server_providers(combiner, "crib") == 0


def test_drop_only_targets_named_server() -> None:
    combiner = FastMCP("parent")
    base = len(combiner.providers)
    mount_server_provider(combiner, _child("a"), "alpha")
    mount_server_provider(combiner, _child("b"), "beta")

    assert drop_server_providers(combiner, "alpha") == 1
    assert len(combiner.providers) == base + 1
    assert drop_server_providers(combiner, "beta") == 1
    assert len(combiner.providers) == base


def test_remount_does_not_duplicate() -> None:
    """Mounting the same namespace twice must not leave two providers
    resolving the same tool names."""
    combiner = FastMCP("parent")
    base = len(combiner.providers)

    mount_server_provider(combiner, _child(), "crib")
    mount_server_provider(combiner, _child(), "crib")

    assert len(combiner.providers) == base + 1
    assert drop_server_providers(combiner, "crib") == 1


def test_drop_sniffs_mounts_that_bypassed_the_registry() -> None:
    """Safety net: a raw combiner.mount() (no bookkeeping) is still dropped."""
    combiner = FastMCP("parent")
    base = len(combiner.providers)
    combiner.mount(_child(), namespace="crib")

    assert drop_server_providers(combiner, "crib") == 1
    assert len(combiner.providers) == base


async def test_restart_leaves_one_live_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full restart flow on a real combiner: after combiner__restart_server the
    namespace has exactly ONE provider and a proxied call round-trips (the
    original bug left a stale provider shadowing the remount, so every call
    failed with 'persistent connection is down')."""
    cfg_path = tmp_path / "servers.json"
    cfg_path.write_text(json.dumps({"mcpServers": {"crib": {"command": "true"}}}))
    config = CombinerConfig.load(str(cfg_path))

    combiner = FastMCP("combiner")
    conn = FakeConnManager()
    ss = FakeSSManager(sharedserver_backed={"crib"})

    proxies: list[FastMCP] = []

    def _make_proxy(*_a: Any, **_k: Any) -> FastMCP:
        proxies.append(_child(f"crib-gen{len(proxies)}"))
        return proxies[-1]

    monkeypatch.setattr(proxyfactory_mod, "_create_server_proxy", _make_proxy)
    monkeypatch.setattr(server_mod, "invalidate_tool_cache", lambda: None)
    monkeypatch.setattr(server_mod, "clear_tool_cache", lambda: None)
    monkeypatch.setattr(toolcache_mod, "invalidate_tool_cache", lambda: None)
    monkeypatch.setattr(toolcache_mod, "clear_tool_cache", lambda: None)

    register_meta_tools(combiner, config, conn, ss)

    base = len(combiner.providers)
    # Startup mount (same helper server.py's lifespan uses).
    mount_server_provider(combiner, _make_proxy(), "crib")

    try:
        async with Client(combiner) as client:
            result = await client.call_tool("combiner__restart_server", {"server_name": "crib"})
            text = result.content[0].text
            assert "restarted" in text
            assert "1 provider(s) replaced" in text

            assert len(combiner.providers) == base + 1

            # The restart's prime stored the namespaced slice (its presence IS
            # the tools-ready bit; the tools/list answer IS the stored set).
            assert [str(t.name) for t in server_mod._server_tool_cache["crib"]] == ["crib_ping"]

            # The proxied call resolves through the NEW proxy, not a stale one.
            pong = await client.call_tool("crib_ping", {})
            assert pong.content[0].text == "pong"
        assert len(proxies) == 2  # startup proxy + restart's fresh proxy
    finally:
        server_mod._server_tool_cache.pop("crib", None)
        server_mod._server_tool_seen.pop("crib", None)
