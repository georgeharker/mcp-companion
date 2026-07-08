"""Tests for per-session server filtering and REST session endpoints.

Tests the following combiner features:
  - _session_disabled dict management
  - _token_sessions ACP token registry
  - REST endpoints: /sessions, /sessions/{id}/filter, /sessions/token/{token}
  - Middleware filtering in on_list_tools and on_call_tool
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

FIXTURES = Path(__file__).parent / "fixtures"


# ── Helpers ────────────────────────────────────────────────────────


def _make_combiner_app():
    """Create a combiner FastMCP app with test config for REST endpoint testing.

    Returns (app, config) where app is the Starlette-compatible ASGI app
    and config is the CombinerConfig used.
    """
    from mcp_combiner.config import CombinerConfig
    from mcp_combiner.server import create_combiner

    config_path = str(FIXTURES / "servers.json")
    config = CombinerConfig.load(config_path)
    combiner = create_combiner(config_path)
    return combiner, config


def _reset_session_state():
    """Reset module-level session state between tests."""
    import mcp_combiner.server as srv

    srv._session_disabled.clear()
    srv._token_sessions.clear()
    srv._pending_token_filters.clear()
    srv._active_sessions.clear()


# ── _session_disabled dict ─────────────────────────────────────────


class TestSessionDisabledDict:
    """Unit tests for the _session_disabled module-level dict."""

    def setup_method(self):
        _reset_session_state()

    def teardown_method(self):
        _reset_session_state()

    def test_empty_by_default(self):
        import mcp_combiner.server as srv

        assert srv._session_disabled == {}

    def test_set_and_get(self):
        import mcp_combiner.server as srv

        srv._session_disabled["session-1"] = {"everything"}
        assert srv._session_disabled.get("session-1") == {"everything"}

    def test_missing_session_returns_none(self):
        import mcp_combiner.server as srv

        assert srv._session_disabled.get("nonexistent") is None

    def test_clear_session(self):
        import mcp_combiner.server as srv

        srv._session_disabled["session-1"] = {"everything"}
        del srv._session_disabled["session-1"]
        assert srv._session_disabled.get("session-1") is None

    def test_multiple_sessions_independent(self):
        import mcp_combiner.server as srv

        srv._session_disabled["session-1"] = {"everything"}
        srv._session_disabled["session-2"] = {"http-example"}
        assert srv._session_disabled["session-1"] == {"everything"}
        assert srv._session_disabled["session-2"] == {"http-example"}


# ── Token registry ─────────────────────────────────────────────────


class TestTokenSessions:
    """Unit tests for the _token_sessions ACP token registry."""

    def setup_method(self):
        _reset_session_state()

    def teardown_method(self):
        _reset_session_state()

    def test_empty_by_default(self):
        import mcp_combiner.server as srv

        assert srv._token_sessions == {}

    def test_set_and_get(self):
        import mcp_combiner.server as srv

        srv._token_sessions["token-abc"] = "combiner-session-xyz"
        assert srv._token_sessions["token-abc"] == "combiner-session-xyz"

    def test_missing_token_returns_none(self):
        import mcp_combiner.server as srv

        assert srv._token_sessions.get("nonexistent") is None

    def test_multiple_tokens_independent(self):
        import mcp_combiner.server as srv

        srv._token_sessions["token-1"] = "session-a"
        srv._token_sessions["token-2"] = "session-b"
        assert srv._token_sessions["token-1"] == "session-a"
        assert srv._token_sessions["token-2"] == "session-b"

    def test_clear_token(self):
        import mcp_combiner.server as srv

        srv._token_sessions["token-1"] = "session-a"
        del srv._token_sessions["token-1"]
        assert "token-1" not in srv._token_sessions


# ── REST endpoint tests ────────────────────────────────────────────


class TestSessionRESTEndpoints:
    """Integration tests for session management REST endpoints.

    Uses Starlette TestClient to test the HTTP layer directly.
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        _reset_session_state()
        yield
        _reset_session_state()

    @pytest.fixture
    def client(self):
        combiner, _config = _make_combiner_app()
        app = combiner.http_app()
        return TestClient(app, raise_server_exceptions=False)

    # -- GET /sessions --

    def test_list_sessions_empty(self, client):
        resp = client.get("/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert "sessions" in data
        assert isinstance(data["sessions"], list)

    # -- GET /sessions/token/{token} --

    def test_token_lookup_found(self, client):
        import mcp_combiner.server as srv

        token = "aaaabbbb-cccc-4ddd-8eee-ffffffffffff"
        srv._token_sessions[token] = "combiner-session-xyz"
        resp = client.get(f"/sessions/token/{token}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["token"] == token
        assert data["session_id"] == "combiner-session-xyz"

    def test_token_lookup_not_found(self, client):
        resp = client.get("/sessions/token/00000000-0000-4000-8000-000000000000")
        assert resp.status_code == 404
        assert "error" in resp.json()

    def test_token_lookup_does_not_remove_entry(self, client):
        import mcp_combiner.server as srv

        token = "aaaabbbb-cccc-4ddd-8eee-ffffffffffff"
        srv._token_sessions[token] = "combiner-session-xyz"
        client.get(f"/sessions/token/{token}")
        # Token remains for subsequent lookups
        assert token in srv._token_sessions

    # -- POST /sessions/{id}/filter --

    def test_post_session_filter(self, client):
        resp = client.post(
            "/sessions/test-session-123/filter",
            json={"disabled_servers": ["everything"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "test-session-123"
        assert "everything" in data["disabled_servers"]

    def test_post_session_filter_unknown_server(self, client):
        resp = client.post(
            "/sessions/test-session-123/filter",
            json={"disabled_servers": ["nonexistent"]},
        )
        assert resp.status_code == 400

    def test_post_session_filter_empty_clears(self, client):
        # Set filter
        client.post(
            "/sessions/test-session-123/filter",
            json={"disabled_servers": ["everything"]},
        )
        # Clear with empty list
        resp = client.post(
            "/sessions/test-session-123/filter",
            json={"disabled_servers": []},
        )
        assert resp.status_code == 200
        assert resp.json()["disabled_servers"] == []

    # -- DELETE /sessions/{id}/filter --

    def test_delete_session_filter(self, client):
        # Set then delete
        client.post(
            "/sessions/test-session-123/filter",
            json={"disabled_servers": ["everything"]},
        )
        resp = client.delete("/sessions/test-session-123/filter")
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "cleared"
        assert "everything" in data["previously_disabled"]

    def test_delete_session_filter_nonexistent(self, client):
        resp = client.delete("/sessions/nonexistent/filter")
        assert resp.status_code == 200
        assert resp.json()["previously_disabled"] == []


# ── Middleware unit tests ──────────────────────────────────────────


class TestMiddlewareFiltering:
    """Unit tests for session filtering in on_list_tools and on_call_tool."""

    def setup_method(self):
        _reset_session_state()

    def teardown_method(self):
        _reset_session_state()

    def test_session_disabled_blocks_tools_lookup(self):
        """Verify that _session_disabled entries are keyed by session_id string."""
        import mcp_combiner.server as srv

        srv._session_disabled["test-sid"] = {"everything"}
        assert "everything" in srv._session_disabled.get("test-sid", set())
        assert srv._session_disabled.get("other-sid") is None


# ── Single-flight tools/list cache fill ────────────────────────────


class TestToolsListSingleFlight:
    """Concurrent tools/list misses should coalesce into one upstream fetch.

    Without single-flight, an N-session ``tools_list_changed`` broadcast
    causes N concurrent ``call_next`` invocations against the same OAuth-backed
    upstream, which races the SDK's auth-context lock.
    """

    def setup_method(self):
        import mcp_combiner.server as srv

        _reset_session_state()
        srv._tool_cache = None
        srv._tool_cache_time = 0
        srv.ToolProcessingMiddleware._inflight = None

    def teardown_method(self):
        import mcp_combiner.server as srv

        srv._tool_cache = None
        srv._tool_cache_time = 0
        srv.ToolProcessingMiddleware._inflight = None
        _reset_session_state()

    @pytest.mark.asyncio
    async def test_concurrent_misses_coalesce(self):
        import asyncio

        from mcp_combiner.server import ToolProcessingMiddleware

        mw = ToolProcessingMiddleware()

        call_count = 0
        gate = asyncio.Event()

        async def fake_call_next(_ctx):
            nonlocal call_count
            call_count += 1
            await gate.wait()  # hold the first fetch open while others queue up
            return []

        ctx = MagicMock()
        ctx.fastmcp_context = None

        tasks = [asyncio.create_task(mw.on_list_tools(ctx, fake_call_next)) for _ in range(10)]
        # Yield so all tasks reach the in-flight join point before we release.
        for _ in range(20):
            await asyncio.sleep(0)
        gate.set()
        results = await asyncio.gather(*tasks)

        assert call_count == 1, f"expected 1 upstream fetch, got {call_count}"
        assert all(r == [] for r in results)

    @pytest.mark.asyncio
    async def test_failure_does_not_wedge_inflight(self):
        """A failed fetch must clear the in-flight slot so the next call retries."""
        from mcp_combiner.server import ToolProcessingMiddleware

        mw = ToolProcessingMiddleware()
        ctx = MagicMock()
        ctx.fastmcp_context = None

        async def failing_fetch(_ctx):
            raise RuntimeError("upstream boom")

        # _do_fetch swallows upstream errors and returns []; the in-flight
        # slot should still be cleared after the call resolves.
        result = await mw.on_list_tools(ctx, failing_fetch)
        assert result == []
        assert ToolProcessingMiddleware._inflight is None

        # A subsequent call must be free to issue a new fetch.
        called = 0

        async def succeeding_fetch(_ctx):
            nonlocal called
            called += 1
            return []

        await mw.on_list_tools(ctx, succeeding_fetch)
        assert called == 1


# ── per-server stale-tool hysteresis ───────────────────────────────


class TestStaleServerTools:
    """`_merge_stale_server_tools` keeps a transiently-absent server's tools.

    Regression cover for the green/red "flapping": the advertised tool set is
    derived, with no hysteresis, from whichever servers return tools in the
    current fetch. Because any one server's reconnect clears the whole cache and
    forces a refetch, a *second* server that is mid-reconnect at that moment
    contributes zero tools and silently drops out (cross-server contamination).
    Re-injecting its last-known slice within a grace window breaks that coupling.
    """

    def setup_method(self):
        import mcp_combiner.server as srv

        self._srv = srv
        self._saved = (srv._combiner_config, srv._conn_manager)
        srv._server_tool_cache.clear()
        srv._server_tool_seen.clear()
        srv._local_tools_ready.clear()

    def teardown_method(self):
        srv = self._srv
        srv._combiner_config, srv._conn_manager = self._saved
        srv._server_tool_cache.clear()
        srv._server_tool_seen.clear()
        srv._local_tools_ready.clear()

    @staticmethod
    def _tool(name: str):
        from fastmcp.tools.function_tool import FunctionTool

        return FunctionTool(
            fn=lambda: None,
            name=name,
            description="",
            parameters={"type": "object", "properties": {}},
        )

    def _config(self, **flags):
        """A stand-in combiner config with servers alpha, beta.

        flags: e.g. beta_disabled=True to mark beta disabled.
        """
        from types import SimpleNamespace

        from mcp_combiner.config import ServerConfig

        servers = {
            "alpha": ServerConfig(name="alpha"),
            "beta": ServerConfig(name="beta", disabled=flags.get("beta_disabled", False)),
        }
        return SimpleNamespace(servers=servers)

    def _names(self, tools):
        return sorted(str(t.name) for t in tools)

    def test_reconnecting_peer_tools_survive(self):
        """beta mid-reconnect (absent from fresh) keeps its tools after alpha reconnects."""
        srv = self._srv
        srv._combiner_config = self._config()
        srv._conn_manager = None

        both = [self._tool("alpha_one"), self._tool("beta_one"), self._tool("beta_two")]
        # First fetch: both live — seeds the per-server cache.
        srv._merge_stale_server_tools(both, now=1000.0)

        # Second fetch a few seconds later: beta is mid-reconnect → only alpha's tool.
        fresh = [self._tool("alpha_one")]
        merged = srv._merge_stale_server_tools(fresh, now=1005.0)

        assert self._names(merged) == ["alpha_one", "beta_one", "beta_two"], (
            "beta's tools should be re-injected from cache while it reconnects"
        )

    def test_disabled_server_tools_dropped(self):
        """A disabled server's stale tools are not re-served (and are evicted)."""
        srv = self._srv
        srv._combiner_config = self._config(beta_disabled=True)
        srv._conn_manager = None

        srv._server_tool_cache["beta"] = [self._tool("beta_one")]
        srv._server_tool_seen["beta"] = 1000.0

        merged = srv._merge_stale_server_tools([self._tool("alpha_one")], now=1005.0)
        assert self._names(merged) == ["alpha_one"]
        assert "beta" not in srv._server_tool_cache

    def test_grace_window_expiry_drops_tools(self):
        """Past STALE_TOOL_GRACE the stale slice is dropped and evicted."""
        srv = self._srv
        srv._combiner_config = self._config()
        srv._conn_manager = None

        srv._server_tool_cache["beta"] = [self._tool("beta_one")]
        srv._server_tool_seen["beta"] = 1000.0

        now = 1000.0 + srv.STALE_TOOL_GRACE + 1
        merged = srv._merge_stale_server_tools([self._tool("alpha_one")], now=now)
        assert self._names(merged) == ["alpha_one"]
        assert "beta" not in srv._server_tool_cache

    def test_auth_failed_server_tools_dropped(self):
        """An auth-failed server's tools are not re-served even within grace."""
        from unittest.mock import MagicMock

        srv = self._srv
        srv._combiner_config = self._config()
        cm = MagicMock()
        cm.is_auth_failed = lambda name: name == "beta"
        srv._conn_manager = cm

        srv._server_tool_cache["beta"] = [self._tool("beta_one")]
        srv._server_tool_seen["beta"] = 1000.0

        merged = srv._merge_stale_server_tools([self._tool("alpha_one")], now=1005.0)
        assert self._names(merged) == ["alpha_one"]
        assert "beta" not in srv._server_tool_cache

    def test_failed_server_tools_dropped(self):
        """Remove on tool attempt: a server with a recorded call failure is not
        re-served, even inside the grace window (its stale slice is dropped)."""
        srv = self._srv
        srv._combiner_config = self._config()
        srv._conn_manager = None
        srv._server_tool_cache["beta"] = [self._tool("beta_one")]
        srv._server_tool_seen["beta"] = 1000.0
        srv._failed_servers["beta"] = "ConnectionError: dead"
        try:
            merged = srv._merge_stale_server_tools([self._tool("alpha_one")], now=1005.0)
            assert self._names(merged) == ["alpha_one"]
            assert "beta" not in srv._server_tool_cache
        finally:
            srv._failed_servers.pop("beta", None)

    def test_present_server_marked_tools_ready(self):
        """A server that returns tools in a fresh fetch is confirmed 'ready'."""
        srv = self._srv
        srv._combiner_config = self._config()
        srv._conn_manager = None
        assert srv._local_tools_ready.get("alpha") is None
        srv._merge_stale_server_tools([self._tool("alpha_one")], now=1000.0)
        assert srv._local_tools_ready.get("alpha") is True

    def test_evicted_server_loses_ready(self):
        """A dropped (disabled/expired) server also loses its ready flag."""
        srv = self._srv
        srv._combiner_config = self._config(beta_disabled=True)
        srv._conn_manager = None
        srv._server_tool_cache["beta"] = [self._tool("beta_one")]
        srv._server_tool_seen["beta"] = 1000.0
        srv._local_tools_ready["beta"] = True
        srv._merge_stale_server_tools([self._tool("alpha_one")], now=1005.0)
        assert "beta" not in srv._local_tools_ready


# ── stdio/sharedserver started → ready lifecycle ───────────────────


class TestLocalToolsReady:
    """`prime_server_tools`: the started → ready transition — invoke tools/list
    on the server's mounted provider; on answer, store the slice, mark ready,
    and broadcast. On timeout: no store, no broadcast, stays 'started'."""

    @staticmethod
    def _combiner_with_provider(name: str, list_tools) -> object:
        """A minimal combiner whose mount registry maps *name* → one provider."""
        from types import SimpleNamespace

        from mcp_combiner import mounts

        combiner = SimpleNamespace(providers=[])
        mounts._registry(combiner)[name] = [SimpleNamespace(list_tools=list_tools)]
        return combiner

    async def test_prime_retries_stores_then_invalidates(self):
        """Retries a not-yet-serving server; stores the slice, marks ready, and
        invalidates only once tools/list answers."""
        import mcp_combiner.server as srv

        srv._local_tools_ready.pop("beta", None)
        calls: list[int] = []
        state = {"n": 0}

        async def list_tools():
            state["n"] += 1
            if state["n"] < 3:
                raise ConnectionError("still starting")
            return [TestStaleServerTools._tool("beta_one")]

        combiner = self._combiner_with_provider("beta", list_tools)
        orig = srv.invalidate_tool_cache
        srv.invalidate_tool_cache = lambda: calls.append(1)  # type: ignore[assignment]
        try:
            ok = await srv.prime_server_tools(combiner, "beta", timeout=10.0, interval=0.0)
            assert ok is True
            assert srv._local_tools_ready.get("beta") is True
            # The primed list IS the stored slice (namespaced provider shape).
            assert [str(t.name) for t in srv._server_tool_cache["beta"]] == ["beta_one"]
            assert calls == [1]  # broadcast exactly once, after the list returned
        finally:
            srv.invalidate_tool_cache = orig  # type: ignore[assignment]
            srv._local_tools_ready.pop("beta", None)
            srv._server_tool_cache.pop("beta", None)
            srv._server_tool_seen.pop("beta", None)

    async def test_prime_timeout_no_store_no_invalidate(self):
        """If the server never answers tools/list, nothing is stored, nothing is
        broadcast, and it stays 'started' (not ready)."""
        import mcp_combiner.server as srv

        srv._local_tools_ready.pop("gamma", None)
        calls: list[int] = []

        async def list_tools():
            raise ConnectionError("never up")

        combiner = self._combiner_with_provider("gamma", list_tools)
        orig = srv.invalidate_tool_cache
        srv.invalidate_tool_cache = lambda: calls.append(1)  # type: ignore[assignment]
        try:
            ok = await srv.prime_server_tools(combiner, "gamma", timeout=0.05, interval=0.0)
            assert ok is False
            assert srv._local_tools_ready.get("gamma") is None
            assert "gamma" not in srv._server_tool_cache
            assert calls == []
        finally:
            srv.invalidate_tool_cache = orig  # type: ignore[assignment]


def test_stale_tool_grace_configurable():
    """create_combiner(stale_tool_grace=…) overrides the grace; omitting it (None)
    leaves the module default (30s) untouched."""
    import mcp_combiner.server as srv

    saved = srv.STALE_TOOL_GRACE
    try:
        srv.STALE_TOOL_GRACE = 30.0
        srv.create_combiner(str(FIXTURES / "servers.json"))  # None → unchanged
        assert srv.STALE_TOOL_GRACE == 30.0
        srv.create_combiner(str(FIXTURES / "servers.json"), stale_tool_grace=7)
        assert srv.STALE_TOOL_GRACE == 7.0
    finally:
        srv.STALE_TOOL_GRACE = saved


# ── unconditional object-schema coercion (issue #7 safety net) ─────


class TestCoerceObjectSchemas:
    """`_coerce_object_schemas` guarantees an object-shaped inputSchema.

    Belt-and-braces for issue #7: a blank / missing-``type`` / non-dict
    ``properties`` schema (an empty Lua table serializes to ``[]``, which strict
    adapters like Copilot reject) is coerced to ``{"type":"object",
    "properties":{}}`` on the FINAL tools/list — independent of the opt-in
    ``schema_fixes`` path, and covering the appended neovim virtual tools.
    """

    @staticmethod
    def _tool(name: str, params):
        from fastmcp.tools.function_tool import FunctionTool

        return FunctionTool(fn=lambda: None, name=name, description="", parameters=params)

    def test_missing_type_and_properties_coerced(self):
        import mcp_combiner.schemafix as srv

        (out,) = srv._coerce_object_schemas([self._tool("neovim_get_cursor", {})])
        assert out.parameters == {"type": "object", "properties": {}}

    def test_missing_properties_filled(self):
        import mcp_combiner.schemafix as srv

        (out,) = srv._coerce_object_schemas([self._tool("t", {"type": "object"})])
        assert out.parameters == {"type": "object", "properties": {}}

    def test_valid_schema_passed_through_unchanged(self):
        import mcp_combiner.schemafix as srv

        good = self._tool("t", {"type": "object", "properties": {"x": {"type": "string"}}})
        (out,) = srv._coerce_object_schemas([good])
        assert out is good  # not rebuilt

    def test_dispatch_fn_preserved_on_rebuild(self):
        """Rebuild must preserve fn so neovim virtual-tool dispatch still works."""
        import mcp_combiner.schemafix as srv

        t = self._tool("neovim_x", {})
        (out,) = srv._coerce_object_schemas([t])
        assert out.fn is t.fn

    def test_string_required_wrapped_in_array(self):
        """A bare-string `required` is coerced to a single-element array."""
        import mcp_combiner.schemafix as srv

        t = self._tool("t", {"type": "object", "properties": {}, "required": "buffer"})
        (out,) = srv._coerce_object_schemas([t])
        assert out.parameters["required"] == ["buffer"]

    def test_uncoercible_required_dropped(self):
        """A non-list, non-string `required` (e.g. a number) is dropped."""
        import mcp_combiner.schemafix as srv

        t = self._tool("t", {"type": "object", "properties": {}, "required": 5})
        (out,) = srv._coerce_object_schemas([t])
        assert "required" not in out.parameters

    def test_valid_required_preserved(self):
        """A list `required` is valid and passes through untouched (not rebuilt)."""
        import mcp_combiner.schemafix as srv

        good = self._tool("t", {"type": "object", "properties": {}, "required": ["x"]})
        (out,) = srv._coerce_object_schemas([good])
        assert out is good
        assert out.parameters["required"] == ["x"]


class TestFinalizeSchemas:
    """`_finalize_schemas` is the SINGLE egress cleanup: global schema_fixes + coerce.

    Regression cover for issue #7's root cause — neovim virtual tools used to be
    appended after the cleanup and so never saw ``schema_fixes``. Now every tool
    (upstream and neovim-named) goes through the same egress function.
    """

    @staticmethod
    def _tool(name: str, params):
        from fastmcp.tools.function_tool import FunctionTool

        return FunctionTool(fn=lambda: None, name=name, description="", parameters=params)

    def test_schema_fixes_reach_a_neovim_named_tool(self):
        """anyof_type_hoist (which coerce never does) is applied to a neovim tool."""
        import mcp_combiner.schemafix as srv
        from mcp_combiner.runtime import RUNTIME

        saved = RUNTIME.schema_fixes
        RUNTIME.schema_fixes = frozenset({"anyof_type_hoist"})
        try:
            t = self._tool(
                "neovim_x",
                {"type": "array", "anyOf": [{"items": {"type": "string"}}, {"type": "null"}]},
            )
            (out,) = srv._finalize_schemas([t])
            # Parent 'array' type was hoisted into the first anyOf item — proof the
            # global schema_fix ran on a neovim-named tool at egress.
            assert out.parameters["anyOf"][0] == {"type": "array", "items": {"type": "string"}}
        finally:
            RUNTIME.schema_fixes = saved

    def test_idempotent_across_repeat_calls(self):
        import mcp_combiner.schemafix as srv
        from mcp_combiner.runtime import RUNTIME

        saved = RUNTIME.schema_fixes
        RUNTIME.schema_fixes = frozenset({"empty_object"})
        try:
            once = srv._finalize_schemas([self._tool("neovim_x", {})])
            twice = srv._finalize_schemas(once)
            assert once[0].parameters == twice[0].parameters == {"type": "object", "properties": {}}
        finally:
            RUNTIME.schema_fixes = saved

    def test_coerce_still_runs_with_no_schema_fixes(self):
        import mcp_combiner.schemafix as srv
        from mcp_combiner.runtime import RUNTIME

        saved = RUNTIME.schema_fixes
        RUNTIME.schema_fixes = frozenset()
        try:
            (out,) = srv._finalize_schemas([self._tool("neovim_x", {})])
            assert out.parameters == {"type": "object", "properties": {}}
        finally:
            RUNTIME.schema_fixes = saved


class TestBuildNvimTools:
    """`_build_nvim_tools` coerces a manifest schema to a valid object.

    The Lua side serializes an empty table to `[]`; this is the combiner-side
    guarantee that a neovim manifest schema of `[]` (or `{}`) still yields an
    object-shaped tool. Complements the Lua `schema.normalize` unit test.
    """

    def test_list_schema_yields_object_tool(self):
        from mcp_combiner.nvim_proxy import _build_nvim_tools

        manifest = {"neovim": {"tools": [{"name": "get_cursor", "inputSchema": []}]}}
        by_name = {str(t.name): t for t in _build_nvim_tools(manifest)}
        params = by_name["neovim_get_cursor"].parameters
        assert params["type"] == "object"
        assert isinstance(params["properties"], dict)
        # The routing arg is injected into a proper properties object.
        assert "nvim_instance" in params["properties"]


class TestOnListToolsIntegration:
    """End-to-end through the real middleware: on_list_tools → append_nvim_tools
    → _finalize_schemas.

    Guards the exact wiring issue #7 broke — a neovim virtual tool with a
    malformed manifest schema must come out clean *on the wire*, and the append
    path must not be able to bypass schema cleanup.
    """

    def setup_method(self):
        import mcp_combiner.nvim_proxy as nvim_proxy
        import mcp_combiner.server as srv

        srv._tool_cache = None
        srv._tool_cache_time = 0
        srv._server_tool_cache.clear()
        srv._server_tool_seen.clear()
        srv.ToolProcessingMiddleware._inflight = None
        from mcp_combiner.runtime import RUNTIME

        self._saved_channel = nvim_proxy._nvim_channel
        self._saved_fixes = RUNTIME.schema_fixes

    def teardown_method(self):
        import mcp_combiner.nvim_proxy as nvim_proxy
        import mcp_combiner.server as srv
        from mcp_combiner.runtime import RUNTIME

        nvim_proxy._nvim_channel = self._saved_channel
        RUNTIME.schema_fixes = self._saved_fixes
        srv._tool_cache = None
        srv.ToolProcessingMiddleware._inflight = None

    @staticmethod
    def _inject_manifest(tools):
        import mcp_combiner.nvim_proxy as nvim_proxy
        from mcp_combiner.nvim_channel import NvimChannelManager

        ch = NvimChannelManager()
        ch._manifest = {"neovim": {"tools": tools, "resources": []}}
        nvim_proxy._nvim_channel = ch

    async def _list(self):
        import mcp_combiner.server as srv

        mw = srv.ToolProcessingMiddleware()
        ctx = MagicMock()
        ctx.fastmcp_context = None

        async def call_next(_ctx):
            return []  # no upstream tools; isolate the neovim path

        tools = await mw.on_list_tools(ctx, call_next)
        return {str(t.name): t for t in tools}

    async def test_neovim_string_required_wrapped_on_the_wire(self):
        """A neovim tool's string `required` is only fixable at egress — proves
        _finalize_schemas runs on the appended neovim tools (the #7 wiring)."""
        self._inject_manifest(
            [
                {
                    "name": "set_cursor",
                    "description": "x",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"line": {"type": "integer"}},
                        "required": "line",
                    },
                }
            ]
        )
        by_name = await self._list()
        assert "neovim_set_cursor" in by_name
        # _build_nvim_tools does NOT touch `required`; only egress cleanup does.
        assert by_name["neovim_set_cursor"].parameters["required"] == ["line"]

    async def test_neovim_list_schema_becomes_object_on_the_wire(self):
        self._inject_manifest([{"name": "list_buffers", "description": "x", "inputSchema": []}])
        by_name = await self._list()
        params = by_name["neovim_list_buffers"].parameters
        assert params["type"] == "object"
        assert isinstance(params["properties"], dict)


class TestOutputSchemaPlumbing:
    """outputSchema is advertised end-to-end, structuredContent is forwarded, and
    a params fix must not drop the declared output schema."""

    @staticmethod
    def _out():
        return {
            "type": "object",
            "properties": {"buffer": {"type": "integer"}},
            "required": ["buffer"],
        }

    def test_build_nvim_tools_carries_output_schema(self):
        from mcp_combiner.nvim_proxy import _build_nvim_tools

        manifest = {
            "neovim": {
                "tools": [
                    {
                        "name": "get_cursor",
                        "inputSchema": {"type": "object", "properties": {}},
                        "outputSchema": self._out(),
                    },
                    {"name": "read_buffer", "inputSchema": {"type": "object", "properties": {}}},
                ]
            }
        }
        by = {str(t.name): t for t in _build_nvim_tools(manifest)}
        assert by["neovim_get_cursor"].output_schema == self._out()
        assert by["neovim_read_buffer"].output_schema is None  # text-only tool

    def test_dispatch_forwards_structured_content(self):
        from mcp_combiner.nvim_proxy import _dispatch_result_to_tool_result

        res = _dispatch_result_to_tool_result(
            {
                "content": [{"type": "text", "text": '{"buffer":3}'}],
                "structuredContent": {"buffer": 3},
            },
            "neovim_get_cursor",
        )
        assert res.structured_content == {"buffer": 3}

    def test_dispatch_text_only_has_no_structured(self):
        from mcp_combiner.nvim_proxy import _dispatch_result_to_tool_result

        res = _dispatch_result_to_tool_result(
            {"content": [{"type": "text", "text": "hi"}]}, "neovim_x"
        )
        assert res.structured_content is None

    def test_normalize_preserves_output_schema_on_rebuild(self):
        """A schema_fix that rebuilds the tool must keep its output_schema."""
        from fastmcp.tools.function_tool import FunctionTool

        import mcp_combiner.schemafix as srv

        # Missing `properties` → empty_object fix changes params → tool is rebuilt.
        t = FunctionTool(
            fn=lambda: None,
            name="neovim_x",
            description="",
            parameters={"type": "object"},
            output_schema=self._out(),
        )
        fixed = srv._normalize_tool_schema(t, frozenset({"empty_object"}))
        assert fixed is not t  # actually rebuilt (params changed)
        assert fixed.parameters.get("properties") == {}  # fix applied
        assert fixed.output_schema == self._out()  # ...and output schema survived


# ── _pending_token_filters dict ────────────────────────────────────


class TestPendingTokenFilters:
    """Unit tests for the _pending_token_filters module-level dict.

    This dict stores filter state for tokens whose ACP clients have not
    yet connected.  When the client later connects via /mcp/<token>,
    TokenRewriteMiddleware applies the pending filter.
    """

    def setup_method(self):
        _reset_session_state()

    def teardown_method(self):
        _reset_session_state()

    def test_empty_by_default(self):
        import mcp_combiner.server as srv

        assert srv._pending_token_filters == {}

    def test_set_and_get(self):
        import mcp_combiner.server as srv

        srv._pending_token_filters["token-abc"] = {"everything"}
        assert srv._pending_token_filters.get("token-abc") == {"everything"}

    def test_missing_token_returns_none(self):
        import mcp_combiner.server as srv

        assert srv._pending_token_filters.get("nonexistent") is None

    def test_clear_pending(self):
        import mcp_combiner.server as srv

        srv._pending_token_filters["token-abc"] = {"everything"}
        del srv._pending_token_filters["token-abc"]
        assert srv._pending_token_filters.get("token-abc") is None

    def test_multiple_tokens_independent(self):
        import mcp_combiner.server as srv

        srv._pending_token_filters["token-1"] = {"everything", "http-example"}
        srv._pending_token_filters["token-2"] = {"http-example"}
        assert srv._pending_token_filters["token-1"] == {"everything", "http-example"}
        assert srv._pending_token_filters["token-2"] == {"http-example"}


# ── Token filter REST endpoints ────────────────────────────────────


class TestTokenFilterRESTEndpoints:
    """Integration tests for /sessions/token/{token}/filter endpoints.

    Tests both the "session already connected" path (token in _token_sessions)
    and the "pending" path (token not yet mapped to a session).
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        _reset_session_state()
        yield
        _reset_session_state()

    @pytest.fixture
    def client(self):
        combiner, _config = _make_combiner_app()
        app = combiner.http_app()
        return TestClient(app, raise_server_exceptions=False)

    # -- GET /sessions/token/{token}/filter --

    def test_get_filter_pending_empty(self, client):
        """GET on unconnected token returns pending=True, empty disabled list."""
        resp = client.get("/sessions/token/tok-aaa/filter")
        assert resp.status_code == 200
        data = resp.json()
        assert data["token"] == "tok-aaa"
        assert data["session_id"] is None
        assert data["pending"] is True
        assert data["disabled_servers"] == []

    def test_get_filter_pending_with_state(self, client):
        """GET on unconnected token returns pending filter state."""
        import mcp_combiner.server as srv

        srv._pending_token_filters["tok-aaa"] = {"everything"}
        resp = client.get("/sessions/token/tok-aaa/filter")
        assert resp.status_code == 200
        data = resp.json()
        assert data["pending"] is True
        assert "everything" in data["disabled_servers"]

    def test_get_filter_connected(self, client):
        """GET on connected token returns real session filter state."""
        import mcp_combiner.server as srv

        srv._token_sessions["tok-bbb"] = "session-123"
        srv._session_disabled["session-123"] = {"http-example"}
        resp = client.get("/sessions/token/tok-bbb/filter")
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "session-123"
        assert "http-example" in data["disabled_servers"]
        assert "pending" not in data

    # -- POST /sessions/token/{token}/filter (pending path) --

    def test_post_filter_pending_disabled_servers(self, client):
        """POST with disabled_servers on unconnected token stores as pending."""
        import mcp_combiner.server as srv

        resp = client.post(
            "/sessions/token/tok-ccc/filter",
            json={"disabled_servers": ["everything"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["pending"] is True
        assert "everything" in data["disabled_servers"]
        # Verify pending dict was populated
        assert "everything" in srv._pending_token_filters.get("tok-ccc", set())

    def test_post_filter_pending_allowed_servers(self, client):
        """POST with allowed_servers on unconnected token inverts to disabled."""
        resp = client.post(
            "/sessions/token/tok-ddd/filter",
            json={"allowed_servers": ["everything"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["pending"] is True
        # everything is allowed, so other enabled servers should be disabled
        assert "everything" not in data["disabled_servers"]

    def test_post_filter_pending_empty_clears(self, client):
        """POST with empty disabled_servers on pending token clears pending."""
        import mcp_combiner.server as srv

        srv._pending_token_filters["tok-eee"] = {"everything"}
        resp = client.post(
            "/sessions/token/tok-eee/filter",
            json={"disabled_servers": []},
        )
        assert resp.status_code == 200
        # Pending should be cleared
        assert srv._pending_token_filters.get("tok-eee") is None

    # -- POST /sessions/token/{token}/filter (connected path) --

    def test_post_filter_connected_applies_immediately(self, client):
        """POST on connected token applies filter to session directly."""
        import mcp_combiner.server as srv

        srv._token_sessions["tok-fff"] = "session-456"
        resp = client.post(
            "/sessions/token/tok-fff/filter",
            json={"disabled_servers": ["everything"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "session-456"
        assert "everything" in data["disabled_servers"]
        # Verify _session_disabled was set
        assert "everything" in srv._session_disabled.get("session-456", set())

    def test_post_filter_connected_enable_single(self, client):
        """POST enable on connected token removes server from disabled set."""
        import mcp_combiner.server as srv

        srv._token_sessions["tok-ggg"] = "session-789"
        srv._session_disabled["session-789"] = {"everything", "http-example"}
        resp = client.post(
            "/sessions/token/tok-ggg/filter",
            json={"enable": "everything"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "everything" not in data["disabled_servers"]
        assert "http-example" in data["disabled_servers"]

    def test_post_filter_connected_disable_single(self, client):
        """POST disable on connected token adds server to disabled set."""
        import mcp_combiner.server as srv

        srv._token_sessions["tok-hhh"] = "session-101"
        resp = client.post(
            "/sessions/token/tok-hhh/filter",
            json={"disable": "everything"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "everything" in data["disabled_servers"]
        assert "everything" in srv._session_disabled.get("session-101", set())

    # -- DELETE /sessions/token/{token}/filter --

    def test_delete_filter_pending_clears(self, client):
        """DELETE on unconnected token clears pending state."""
        import mcp_combiner.server as srv

        srv._pending_token_filters["tok-iii"] = {"everything"}
        resp = client.delete("/sessions/token/tok-iii/filter")
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "cleared"
        assert data["session_id"] is None
        assert srv._pending_token_filters.get("tok-iii") is None

    def test_delete_filter_connected_clears(self, client):
        """DELETE on connected token clears the session filter."""
        import mcp_combiner.server as srv

        srv._token_sessions["tok-jjj"] = "session-202"
        srv._session_disabled["session-202"] = {"everything"}
        resp = client.delete("/sessions/token/tok-jjj/filter")
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "cleared"
        assert "everything" in data["previously_disabled"]
        assert srv._session_disabled.get("session-202") is None


# ── allowed_servers → disabled inversion ───────────────────────────


class TestAllowedToDisabledConversion:
    """Tests for the allowed_servers → disabled_servers inversion logic
    in POST /sessions/{id}/filter.

    The fixture config has servers: everything, disabled-server,
    http-example, sharedserver-example.  Only 'everything' is enabled
    by default.
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        _reset_session_state()
        yield
        _reset_session_state()

    @pytest.fixture
    def client(self):
        combiner, _config = _make_combiner_app()
        app = combiner.http_app()
        return TestClient(app, raise_server_exceptions=False)

    def test_allowed_inverts_to_disabled(self, client):
        """allowed_servers=['everything'] disables all others."""
        resp = client.post(
            "/sessions/test-session/filter",
            json={"allowed_servers": ["everything"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        disabled = data["disabled_servers"]
        assert "everything" not in disabled
        # Other configured servers should be disabled
        for name in ("disabled-server", "http-example", "sharedserver-example"):
            assert name in disabled

    def test_allowed_empty_disables_all(self, client):
        """allowed_servers=[] disables every server."""
        resp = client.post(
            "/sessions/test-session/filter",
            json={"allowed_servers": []},
        )
        assert resp.status_code == 200
        data = resp.json()
        disabled = data["disabled_servers"]
        for name in ("everything", "disabled-server", "http-example", "sharedserver-example"):
            assert name in disabled

    def test_allowed_all_disables_none(self, client):
        """allowed_servers with all servers disables nothing."""
        all_servers = ["everything", "disabled-server", "http-example", "sharedserver-example"]
        resp = client.post(
            "/sessions/test-session/filter",
            json={"allowed_servers": all_servers},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["disabled_servers"] == []

    def test_allowed_invalid_type(self, client):
        """allowed_servers must be a list."""
        resp = client.post(
            "/sessions/test-session/filter",
            json={"allowed_servers": "everything"},
        )
        assert resp.status_code == 400

    def test_disabled_unknown_server_rejected(self, client):
        """disabled_servers with unknown server names returns 400."""
        resp = client.post(
            "/sessions/test-session/filter",
            json={"disabled_servers": ["nonexistent-server"]},
        )
        assert resp.status_code == 400
        assert "Unknown servers" in resp.json()["error"]
