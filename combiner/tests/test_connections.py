"""Tests for ConnectionManager lifecycle-event semantics.

The manager exposes two distinct callbacks that must not be conflated:
  - ``on_connection_success`` — session established (MCP initialize done).
  - ``on_tools_ready``        — the upstream's tools are actually listable.

Invalidation must hang off the *latter*: firing it on mere connection-open
races the upstream's tool warm-up and can cache an incomplete tool set until
the TTL expires. ``_signal_tools_ready`` is the bridge — it primes one bounded
``tools/list`` and only signals readiness when that returns.
"""

from __future__ import annotations

from types import SimpleNamespace

from mcp_combiner.connections import ConnectionManager


def _fake_conn(name: str, client: object) -> SimpleNamespace:
    """Minimal stand-in for _ManagedConnection (only fields _signal_tools_ready reads)."""
    return SimpleNamespace(name=name, client_ref=[client])


class TestSignalToolsReady:
    async def test_signals_ready_when_list_succeeds(self):
        """A successful priming tools/list fires on_tools_ready exactly once."""
        ready: list[str] = []

        async def list_tools():
            return []

        mgr = ConnectionManager(on_tools_ready=lambda name: ready.append(name))
        await mgr._signal_tools_ready(_fake_conn("alpha", SimpleNamespace(list_tools=list_tools)))

        assert ready == ["alpha"]

    async def test_does_not_signal_when_list_fails(self):
        """If priming tools/list raises, readiness is NOT signalled (tools unconfirmed)."""
        ready: list[str] = []

        async def list_tools():
            raise RuntimeError("upstream still warming up")

        mgr = ConnectionManager(on_tools_ready=lambda name: ready.append(name))
        await mgr._signal_tools_ready(_fake_conn("beta", SimpleNamespace(list_tools=list_tools)))

        assert ready == []

    async def test_no_client_is_noop(self):
        """A missing client (connection gone) never signals readiness."""
        ready: list[str] = []
        mgr = ConnectionManager(on_tools_ready=lambda name: ready.append(name))
        await mgr._signal_tools_ready(_fake_conn("gamma", None))
        assert ready == []

    async def test_no_callback_is_noop(self):
        """With no on_tools_ready wired, priming is a harmless no-op."""

        called = False

        async def list_tools():
            nonlocal called
            called = True
            return []

        mgr = ConnectionManager()  # no callbacks
        await mgr._signal_tools_ready(_fake_conn("delta", SimpleNamespace(list_tools=list_tools)))
        # Short-circuits before touching the client when there's nothing to signal.
        assert called is False

    async def test_callback_exception_is_swallowed(self):
        """A raising on_tools_ready must not propagate out of _signal_tools_ready."""

        async def list_tools():
            return []

        def boom(_name):
            raise ValueError("cache invalidation blew up")

        mgr = ConnectionManager(on_tools_ready=boom)
        # Must not raise.
        await mgr._signal_tools_ready(_fake_conn("epsilon", SimpleNamespace(list_tools=list_tools)))


def _inject_conn(
    mgr: ConnectionManager,
    name: str,
    *,
    auth_failed: bool = False,
    connected: bool = False,
    tools_ready: bool = False,
    has_client: bool = True,
) -> None:
    """Inject a minimal fake managed-connection so lifecycle_state can read it."""
    client = SimpleNamespace(is_connected=lambda: connected) if has_client else None
    mgr._connections[name] = SimpleNamespace(  # type: ignore[assignment]
        _auth_failed=auth_failed, client_ref=[client], _tools_ready=tools_ready
    )


class TestLifecycleState:
    def test_unknown_when_not_managed(self):
        assert ConnectionManager().lifecycle_state("nope") == "unknown"

    def test_auth_failed_takes_precedence(self):
        mgr = ConnectionManager()
        _inject_conn(mgr, "a", auth_failed=True, connected=True, tools_ready=True)
        assert mgr.lifecycle_state("a") == "auth_failed"

    def test_ready_when_connected_and_tools_ready(self):
        mgr = ConnectionManager()
        _inject_conn(mgr, "a", connected=True, tools_ready=True)
        assert mgr.lifecycle_state("a") == "ready"

    def test_connected_when_tools_not_ready(self):
        mgr = ConnectionManager()
        _inject_conn(mgr, "a", connected=True, tools_ready=False)
        assert mgr.lifecycle_state("a") == "connected"

    def test_disconnected_when_client_not_connected(self):
        mgr = ConnectionManager()
        _inject_conn(mgr, "a", connected=False, tools_ready=True)
        assert mgr.lifecycle_state("a") == "disconnected"

    def test_disconnected_when_no_client(self):
        mgr = ConnectionManager()
        _inject_conn(mgr, "a", has_client=False, tools_ready=True)
        assert mgr.lifecycle_state("a") == "disconnected"

    def test_mark_tools_unready_downgrades_ready_to_connected(self):
        mgr = ConnectionManager()
        _inject_conn(mgr, "a", connected=True, tools_ready=True)
        assert mgr.lifecycle_state("a") == "ready"
        mgr.mark_tools_unready("a")
        assert mgr.lifecycle_state("a") == "connected"

    def test_mark_tools_unready_unknown_is_noop(self):
        ConnectionManager().mark_tools_unready("nope")  # must not raise


class TestBuildServerStatus:
    """build_server_status is the single source of truth for /health + combiner__status."""

    def _config(self):
        from pathlib import Path

        from mcp_combiner.config import CombinerConfig

        return CombinerConfig.load(str(Path(__file__).parent / "fixtures" / "servers.json"))

    def test_disabled_server_reports_disabled(self):
        from mcp_combiner.server import build_server_status

        cfg = self._config()
        assert build_server_status(cfg, ConnectionManager(), "disabled-server").state == "disabled"

    def test_stdio_server_reports_ready(self):
        """A stdio server has no connection lifecycle: mounted ⇒ ready."""
        from mcp_combiner.server import build_server_status

        cfg = self._config()
        # 'everything' is a stdio server in the fixture, not connection-managed.
        assert build_server_status(cfg, ConnectionManager(), "everything").state == "ready"

    def test_http_server_reflects_connection_lifecycle(self):
        from mcp_combiner.server import build_server_status

        cfg = self._config()
        cfg.servers["http-example"].disabled = False  # enable so lifecycle drives state
        mgr = ConnectionManager()
        _inject_conn(mgr, "http-example", connected=True, tools_ready=True)
        assert build_server_status(cfg, mgr, "http-example").state == "ready"

        mgr.mark_tools_unready("http-example")
        assert build_server_status(cfg, mgr, "http-example").state == "connected"

    def test_stdio_crash_reports_disconnected(self):
        """A stdio server with a recorded call failure reads disconnected, not ready."""
        import mcp_combiner.server as srv
        from mcp_combiner.server import build_server_status

        cfg = self._config()
        srv._failed_servers.pop("everything", None)
        try:
            # Healthy stdio server → ready.
            assert build_server_status(cfg, ConnectionManager(), "everything").state == "ready"
            # Crashed subprocess recorded → disconnected.
            srv._failed_servers["everything"] = "ClosedResourceError: stdio pipe closed"
            got = build_server_status(cfg, ConnectionManager(), "everything").state
            assert got == "disconnected"
        finally:
            srv._failed_servers.pop("everything", None)

    def test_call_failure_overrides_optimistic_http_ready(self):
        """A recorded failure downgrades an otherwise-'ready' HTTP server."""
        import mcp_combiner.server as srv
        from mcp_combiner.server import build_server_status

        cfg = self._config()
        cfg.servers["http-example"].disabled = False
        mgr = ConnectionManager()
        _inject_conn(mgr, "http-example", connected=True, tools_ready=True)
        srv._failed_servers.pop("http-example", None)
        try:
            assert build_server_status(cfg, mgr, "http-example").state == "ready"
            srv._failed_servers["http-example"] = "ConnectionError: reset"
            assert build_server_status(cfg, mgr, "http-example").state == "disconnected"
        finally:
            srv._failed_servers.pop("http-example", None)
