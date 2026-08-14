"""Unit tests for the token-keyed isolated-session registry.

E2E coverage of the token-keyed cache behavior (reattach on reconnect,
concurrent same-token sharing, per-token isolation) lives in
test_session_independence.py::TestIsolatedUpstreamSessions; these tests pin
the registry mechanics directly: grace-window expiry, server eviction, and
that expiry never fires while a downstream session is live.
"""

from __future__ import annotations

from typing import Any

import pytest

from mcp_combiner.isolated import GRACE_SECONDS, IsolatedSessionRegistry


class FakeClient:
    def __init__(self) -> None:
        self.disconnected = False

    async def _disconnect(self, force: bool = False) -> None:
        self.disconnected = True


@pytest.fixture
def registry() -> IsolatedSessionRegistry:
    return IsolatedSessionRegistry()


class TestRegistry:
    def test_acquire_miss_then_register_then_hit(
        self, registry: IsolatedSessionRegistry
    ) -> None:
        assert registry.acquire("srv", "tok", None) is None
        client: Any = FakeClient()
        registry.register("srv", "tok", client, None)
        entry = registry.acquire("srv", "tok", None)
        assert entry is not None and entry.client is client
        # Distinct token or server: miss.
        assert registry.acquire("srv", "other", None) is None
        assert registry.acquire("other", "tok", None) is None

    async def test_sweep_expires_only_past_grace(
        self, registry: IsolatedSessionRegistry
    ) -> None:
        client: Any = FakeClient()
        entry = registry.register("srv", "tok", client, None)
        base = entry.last_activity

        # Inside the grace window: kept.
        assert await registry.sweep_once(now=base + GRACE_SECONDS / 2) == 0
        assert not client.disconnected

        # Past it: disconnected and dropped.
        assert await registry.sweep_once(now=base + GRACE_SECONDS + 1) == 1
        assert client.disconnected
        assert registry.acquire("srv", "tok", None) is None

    async def test_acquire_refreshes_grace_clock(
        self, registry: IsolatedSessionRegistry
    ) -> None:
        import time

        client: Any = FakeClient()
        entry = registry.register("srv", "tok", client, None)
        # Backdate the entry past the grace window, then acquire: the acquire
        # must re-stamp last_activity so the next sweep keeps the entry.
        entry.last_activity -= GRACE_SECONDS + 60
        registry.acquire("srv", "tok", None)
        assert await registry.sweep_once(now=time.monotonic()) == 0
        assert not client.disconnected

    async def test_evict_server_drops_only_that_server(
        self, registry: IsolatedSessionRegistry
    ) -> None:
        a: Any = FakeClient()
        b: Any = FakeClient()
        other: Any = FakeClient()
        registry.register("srv", "tok1", a, None)
        registry.register("srv", "tok2", b, None)
        registry.register("keep", "tok1", other, None)

        assert await registry.evict_server("srv") == 2
        assert a.disconnected and b.disconnected
        assert not other.disconnected
        assert registry.acquire("keep", "tok1", None) is not None

    async def test_close_all(self, registry: IsolatedSessionRegistry) -> None:
        a: Any = FakeClient()
        b: Any = FakeClient()
        registry.register("s1", "t1", a, None)
        registry.register("s2", "t2", b, None)
        await registry.close_all()
        assert a.disconnected and b.disconnected
        assert not registry.live
