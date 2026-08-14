"""Unit tests for the token-keyed isolated-session registry.

E2E coverage of the token-keyed cache behavior (reattach on reconnect,
concurrent same-token sharing, per-token isolation) lives in
test_session_independence.py::TestIsolatedUpstreamSessions, and the seeded
resume round-trip in test_resume_transport.py; these tests pin the registry
mechanics directly: grace-window parking, TTL forgetting, server eviction,
and the resumable-vs-plain park split.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from mcp_combiner.isolated import (
    GRACE_SECONDS,
    PARK_TTL_SECONDS,
    IsolatedSessionRegistry,
    ParkedSession,
)
from mcp_combiner.resume_transport import ResumableStreamableHttpTransport


class FakeClient:
    """Duck-typed stand-in: transport + disconnect + initialize_result."""

    def __init__(self, transport: Any = None) -> None:
        self.transport = transport
        self.initialize_result = None
        self.disconnected = False

    async def _disconnect(self, force: bool = False) -> None:
        self.disconnected = True


def _resumable_client(session_id: str = "sid-1234567890") -> FakeClient:
    t = ResumableStreamableHttpTransport("http://127.0.0.1:1/mcp")
    t.resume_session_id = session_id
    t.resume_protocol_version = "2025-06-18"
    return FakeClient(transport=t)


@pytest.fixture
def registry() -> IsolatedSessionRegistry:
    return IsolatedSessionRegistry()


class TestLiveTier:
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

    async def test_acquire_refreshes_grace_clock(
        self, registry: IsolatedSessionRegistry
    ) -> None:
        client: Any = FakeClient()
        entry = registry.register("srv", "tok", client, None)
        # Backdate the entry past the grace window, then acquire: the acquire
        # must re-stamp last_activity so the next sweep keeps the entry.
        entry.last_activity -= GRACE_SECONDS + 60
        registry.acquire("srv", "tok", None)
        assert await registry.sweep_once(now=time.monotonic()) == 0
        assert not client.disconnected


class TestParkAndForget:
    async def test_grace_expiry_parks_resumable_client(
        self, registry: IsolatedSessionRegistry
    ) -> None:
        client: Any = _resumable_client("sid-abc")
        entry = registry.register("srv", "tok", client, None)
        base = entry.last_activity

        # Inside the grace window: untouched.
        assert await registry.sweep_once(now=base + registry.grace / 2) == 0
        assert not client.disconnected

        # Past it: disconnected WITHOUT termination, id parked.
        assert await registry.sweep_once(now=base + registry.grace + 1) == 1
        assert client.disconnected
        assert client.transport.terminate_on_close is False
        parked = registry.pop_parked("srv", "tok")
        assert parked is not None
        assert parked.session_id == "sid-abc"
        assert parked.protocol_version == "2025-06-18"

    async def test_grace_expiry_tears_down_plain_client(
        self, registry: IsolatedSessionRegistry
    ) -> None:
        """No resumable transport (e.g. SSE): expiry is a full disconnect and
        nothing is parked — the pre-token-keyed behavior."""
        client: Any = FakeClient()
        entry = registry.register("srv", "tok", client, None)
        assert await registry.sweep_once(now=entry.last_activity + registry.grace + 1) == 1
        assert client.disconnected
        assert registry.pop_parked("srv", "tok") is None

    async def test_ttl_forgets_parked_entry(
        self, registry: IsolatedSessionRegistry
    ) -> None:
        parked = ParkedSession(
            session_id="sid-old", protocol_version=None, url="http://127.0.0.1:1/mcp"
        )
        registry.restore_parked("srv", "tok", parked)

        assert await registry.sweep_once(now=parked.parked_at + registry.ttl / 2) == 0
        assert registry.parked

        # Past TTL: dropped (the upstream DELETE is best-effort; the bogus
        # url above simply fails silently).
        assert await registry.sweep_once(now=parked.parked_at + registry.ttl + 1) == 1
        assert not registry.parked

    async def test_close_all_park_mode_keeps_sessions_resumable(
        self, registry: IsolatedSessionRegistry
    ) -> None:
        """The sanctioned-handover shutdown: everything parks, nothing is
        terminated — the parked map is what travels to the successor."""
        client: Any = _resumable_client("sid-h")
        registry.register("srv", "tok", client, None)
        await registry.close_all(terminate=False)
        assert client.disconnected
        assert client.transport.terminate_on_close is False
        assert registry.parked[("srv", "tok")].session_id == "sid-h"

    async def test_close_all_default_terminates(
        self, registry: IsolatedSessionRegistry
    ) -> None:
        client: Any = _resumable_client("sid-t")
        registry.register("srv", "tok", client, None)
        await registry.close_all()
        assert client.disconnected
        # terminate_on_close untouched → the transport's close-time DELETE ran.
        assert client.transport.terminate_on_close is True
        assert not registry.parked


class TestEviction:
    async def test_evict_server_drops_live_and_parked(
        self, registry: IsolatedSessionRegistry
    ) -> None:
        a: Any = _resumable_client("sid-a")
        other: Any = FakeClient()
        registry.register("srv", "tok1", a, None)
        registry.register("keep", "tok1", other, None)
        registry.restore_parked(
            "srv",
            "tok2",
            ParkedSession(session_id="sid-b", protocol_version=None, url="http://x/mcp"),
        )

        assert await registry.evict_server("srv") == 2
        assert a.disconnected
        # Dead server: never DELETE, never park — the sessions died with it.
        assert a.transport.terminate_on_close is False
        assert registry.pop_parked("srv", "tok2") is None
        assert not other.disconnected
        assert registry.acquire("keep", "tok1", None) is not None

    def test_configure_and_reset(self, registry: IsolatedSessionRegistry) -> None:
        registry.configure(grace=10.0, ttl=20.0)
        assert (registry.grace, registry.ttl) == (10.0, 20.0)
        registry.reset()
        assert (registry.grace, registry.ttl) == (GRACE_SECONDS, PARK_TTL_SECONDS)
