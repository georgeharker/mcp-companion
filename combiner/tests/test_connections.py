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

from mcp_combiner.connections import (
    _BACKING_RESTART_AFTER,
    _MAX_BACKOFF_LOCAL,
    ConnectionManager,
    _is_local_upstream,
)


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


class TestMonitorRePrime:
    """The health-check monitor re-primes a connected-but-not-ready connection,
    so a session that came up while the upstream was still warming (or whose
    restart raced the respawn) eventually reaches 'ready' and fires
    on_tools_ready — instead of staying stuck until a disconnect."""

    async def test_monitor_reprimes_connected_but_not_ready(self, monkeypatch):
        import asyncio

        import mcp_combiner.connections as conns

        # Spin the monitor fast; we cancel it as soon as it re-primes.
        monkeypatch.setattr(conns, "_HEALTH_CHECK_INTERVAL", 0.0)

        fired: list[str] = []
        mgr = ConnectionManager(on_tools_ready=lambda name: fired.append(name))

        async def list_tools():
            return []

        async def ping():
            return None

        client = SimpleNamespace(is_connected=lambda: True, list_tools=list_tools, ping=ping)
        conn = SimpleNamespace(
            name="a", _auth_failed=False, client_ref=[client], _tools_ready=False
        )

        task = asyncio.create_task(mgr._monitor(conn))
        try:
            for _ in range(200):
                if conn._tools_ready:
                    break
                await asyncio.sleep(0.001)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Re-primed: reached ready and fired on_tools_ready (at least once).
        assert conn._tools_ready is True
        assert "a" in fired

    async def test_monitor_reconnects_when_prime_fails_on_stale_session(self, monkeypatch):
        """A session that claims is_connected() but can't serve tools/list (the
        upstream restarted; the old session 404s everything) must fall through
        to the ping and reconnect — not re-prime forever stuck at 'connected'."""
        import asyncio

        import mcp_combiner.connections as conns

        monkeypatch.setattr(conns, "_HEALTH_CHECK_INTERVAL", 0.0)

        mgr = ConnectionManager()

        async def stale(*_a, **_kw):
            raise RuntimeError("Session terminated")

        client = SimpleNamespace(is_connected=lambda: True, list_tools=stale, ping=stale)
        conn = SimpleNamespace(
            name="a", _auth_failed=False, client_ref=[client], _tools_ready=False
        )

        reconnected = asyncio.Event()

        async def fake_reconnect(_conn):
            reconnected.set()

        monkeypatch.setattr(mgr, "_reconnect", fake_reconnect)

        task = asyncio.create_task(mgr._monitor(conn))
        try:
            await asyncio.wait_for(reconnected.wait(), timeout=2.0)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert reconnected.is_set()


def _reconnect_conn(*, shared_server: str | None, url: str = "http://127.0.0.1:1/mcp"):
    """Minimal stand-in for _ManagedConnection covering what _reconnect touches.

    _backoff starts at 0 so the test never actually sleeps.
    """
    from contextlib import AsyncExitStack

    return SimpleNamespace(
        name="a",
        srv=SimpleNamespace(shared_server=shared_server, url=url),
        stack=AsyncExitStack(),
        client_ref=[None],
        _backoff=0.0,
        _tools_ready=False,
        _consec_failures=0,
    )


class TestReconnectBackingRestart:
    """After _BACKING_RESTART_AFTER consecutive failed opens on a
    sharedserver-backed upstream, _reconnect escalates to restart_backing —
    re-dialing a dead process forever can never recover it."""

    async def test_restarts_backing_after_threshold(self, monkeypatch):
        restarts: list[str] = []

        async def restart_backing(name: str) -> bool:
            restarts.append(name)
            return True

        mgr = ConnectionManager(restart_backing=restart_backing)

        async def failing_open(conn):
            conn.client_ref[0] = None

        monkeypatch.setattr(mgr, "_open", failing_open)
        conn = _reconnect_conn(shared_server="crib")

        for _ in range(_BACKING_RESTART_AFTER):
            await mgr._reconnect(conn)
        assert restarts == []  # threshold not yet reached going in

        await mgr._reconnect(conn)
        assert restarts == ["a"]
        # Counter was reset by the escalation; the next reconnect doesn't
        # immediately restart again.
        await mgr._reconnect(conn)
        assert restarts == ["a"]

    async def test_no_restart_without_shared_server(self, monkeypatch):
        restarts: list[str] = []

        async def restart_backing(name: str) -> bool:
            restarts.append(name)
            return True

        mgr = ConnectionManager(restart_backing=restart_backing)

        async def failing_open(conn):
            conn.client_ref[0] = None

        monkeypatch.setattr(mgr, "_open", failing_open)
        conn = _reconnect_conn(shared_server=None)

        for _ in range(_BACKING_RESTART_AFTER * 2):
            await mgr._reconnect(conn)
        assert restarts == []

    async def test_success_resets_failure_counter(self, monkeypatch):
        restarts: list[str] = []

        async def restart_backing(name: str) -> bool:
            restarts.append(name)
            return True

        mgr = ConnectionManager(restart_backing=restart_backing)
        conn = _reconnect_conn(shared_server="crib")

        async def failing_open(c):
            c.client_ref[0] = None

        async def succeeding_open(c):
            c.client_ref[0] = SimpleNamespace(is_connected=lambda: True)

        monkeypatch.setattr(mgr, "_open", failing_open)
        for _ in range(_BACKING_RESTART_AFTER - 1):
            await mgr._reconnect(conn)

        monkeypatch.setattr(mgr, "_open", succeeding_open)
        await mgr._reconnect(conn)
        assert conn._consec_failures == 0

        # Failures after a success start counting from zero again.
        monkeypatch.setattr(mgr, "_open", failing_open)
        await mgr._reconnect(conn)
        assert restarts == []


class TestLocalBackoffCap:
    def test_is_local_upstream(self):
        assert _is_local_upstream(SimpleNamespace(shared_server="crib", url=None))
        assert _is_local_upstream(
            SimpleNamespace(shared_server=None, url="http://127.0.0.1:7732/mcp")
        )
        assert _is_local_upstream(
            SimpleNamespace(shared_server=None, url="http://localhost:8002/mcp")
        )
        assert not _is_local_upstream(
            SimpleNamespace(shared_server=None, url="https://mcp.clickup.com/mcp")
        )
        assert not _is_local_upstream(SimpleNamespace(shared_server=None, url=None))

    async def test_local_backoff_capped(self, monkeypatch):
        """A repeatedly-failing local upstream never backs off past
        _MAX_BACKOFF_LOCAL — a bounced localhost process should be re-dialed
        in seconds, not after the remote-grade 60s cap."""
        mgr = ConnectionManager()

        async def failing_open(conn):
            conn.client_ref[0] = None

        monkeypatch.setattr(mgr, "_open", failing_open)
        conn = _reconnect_conn(shared_server=None, url="http://localhost:7732/mcp")
        conn._backoff = 8.0

        async def no_sleep(_delay):
            return None

        import mcp_combiner.connections as conns

        monkeypatch.setattr(conns.asyncio, "sleep", no_sleep)

        for _ in range(6):
            await mgr._reconnect(conn)
            assert conn._backoff <= _MAX_BACKOFF_LOCAL


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

    def test_stdio_server_tristate(self):
        """A stdio server is 'connected' (started) until its tools are confirmed
        listable, then 'ready' — the same tri-state HTTP servers get. Mounting
        alone is NOT assumed ready."""
        import mcp_combiner.server as srv
        from mcp_combiner.server import build_server_status

        cfg = self._config()
        # 'everything' is a stdio server in the fixture, not connection-managed.
        srv._local_tools_ready.pop("everything", None)
        try:
            assert build_server_status(cfg, ConnectionManager(), "everything").state == "connected"
            srv._local_tools_ready["everything"] = True
            assert build_server_status(cfg, ConnectionManager(), "everything").state == "ready"
        finally:
            srv._local_tools_ready.pop("everything", None)

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
        srv._local_tools_ready["everything"] = True
        try:
            # Healthy, tools-confirmed stdio server → ready.
            assert build_server_status(cfg, ConnectionManager(), "everything").state == "ready"
            # Crashed subprocess recorded → disconnected (overrides ready).
            srv._failed_servers["everything"] = "ClosedResourceError: stdio pipe closed"
            got = build_server_status(cfg, ConnectionManager(), "everything").state
            assert got == "disconnected"
        finally:
            srv._failed_servers.pop("everything", None)
            srv._local_tools_ready.pop("everything", None)

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
