"""The tools/list wedge: bounded aggregate fetch + a single-flight that cannot strand.

After an external combiner reset, one half-dead upstream provider made the
aggregate ``tools/list`` fetch await forever; every downstream session then
joined a future that never resolved and pi surfaced endless "stale connection"
warnings until the process was bounced. Regression coverage for the two fixes
(see COMBINER-RESTART-BUG.md, follow-up 2026-10-20):

- the mounted provider's listing is time-bounded (``BoundedListProvider``), so
  one wedged upstream is skipped from the aggregate instead of wedging it;
- the single-flight owner is bounded and falls back to last-known-good tools;
- a joiner never hangs on a future whose owner died or was cancelled.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Sequence
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.tools import Tool

import mcp_combiner.server as srv
from mcp_combiner.runtime import RUNTIME

# Small bounds so tests exercise the timeouts quickly.
_FETCH_TIMEOUT = 0.05
_LIST_TIMEOUT = 0.05


class _HangChild(FastMCP):
    """A mounted upstream whose list_tools waits forever — the wedged state."""

    async def list_tools(self) -> Sequence[Tool]:  # type: ignore[override]
        await asyncio.Event().wait()  # never set
        return []


def _reset_inflight_and_cache() -> None:
    RUNTIME.tools.cache = None
    RUNTIME.tools.cache_time = 0
    srv.ToolProcessingMiddleware._inflight = None
    srv.ToolProcessingMiddleware.FETCH_TIMEOUT = _FETCH_TIMEOUT


def _restore_inflight_and_cache() -> None:
    RUNTIME.tools.cache = None
    RUNTIME.tools.cache_time = 0
    srv.ToolProcessingMiddleware._inflight = None
    srv.ToolProcessingMiddleware.FETCH_TIMEOUT = 60.0


def _context() -> MagicMock:
    # fastmcp_context=None keeps the session-filter hooks inert.
    ctx = MagicMock()
    ctx.fastmcp_context = None
    return ctx


async def _hung_fetch(context: MiddlewareContext[Any]) -> Sequence[Tool]:
    gate = getattr(_hung_fetch, "_gate")
    return await cast(Awaitable[Sequence[Tool]], gate.wait())


async def _never_fetch(context: MiddlewareContext[Any]) -> Sequence[Tool]:
    raise AssertionError("joiner must not fetch on its own")


# ── BoundedListProvider (per-provider listing bound) ───────────────


class TestBoundedListProvider:
    """``mount_server_provider`` wraps mounted providers with the listing bound."""

    def test_real_mount_yields_bounded_providers(self) -> None:
        from mcp_combiner.mounts import (
            BoundedListProvider,
            drop_server_providers,
            get_server_providers,
            mount_server_provider,
        )

        combiner = FastMCP("agg")
        child = FastMCP("child")

        @child.tool()
        def ping() -> str:
            return "pong"

        mount_server_provider(combiner, child, "x")
        # add_provider wraps: _WrappedProvider(Bounded(raw), [Namespace]).
        wrapped = combiner.providers[-1]
        assert type(wrapped).__name__ == "_WrappedProvider"
        bounded = cast(BoundedListProvider, getattr(wrapped, "_inner"))
        assert isinstance(bounded, BoundedListProvider)
        assert get_server_providers(combiner, "x") == [combiner.providers[-1]]
        # The namespace transform is OUTSIDE the bound, and visible for sniffing.
        assert [type(t).__name__ for t in bounded.transforms] == []
        assert [type(t).__name__ for t in wrapped.transforms] == ["Namespace"]
        # Drop the wrapper by identity, exactly as unmount does.
        assert drop_server_providers(combiner, "x") == 1
        assert all(p is not wrapped for p in combiner.providers)

    async def test_mount_roundtrip_list_and_call(self) -> None:
        """End-to-end: the bounded mount lists AND routes calls correctly."""
        from mcp_combiner.mounts import mount_server_provider

        combiner = FastMCP("agg")
        child = FastMCP("child")

        @child.tool()
        def mock_whoami() -> str:
            return "i-am-child"

        mount_server_provider(combiner, child, "mockup")
        tools = await combiner.list_tools()
        assert [t.name for t in tools] == ["mockup_mock_whoami"]
        async with Client(combiner) as c:
            result = await c.call_tool("mockup_mock_whoami", {})
            assert result.content[0].model_dump_json() is not None

    async def test_hung_provider_skipped_not_wedged(self) -> None:
        """A mounted, hung upstream is skipped by the aggregate fetch."""
        from mcp_combiner import toolcache as toolcache_mod
        from mcp_combiner.mounts import BoundedListProvider, mount_server_provider

        hung = _HangChild("hung")

        @hung.tool()
        def never_listed() -> str:
            return "nope"

        combiner = FastMCP("agg")
        child = FastMCP("child")

        @child.tool()
        def ping() -> str:
            return "pong"

        original = toolcache_mod.UPSTREAM_TOOL_LIST_TIMEOUT
        toolcache_mod.UPSTREAM_TOOL_LIST_TIMEOUT = _LIST_TIMEOUT
        try:
            # mount_server_provider takes the bound from toolcache at wrap time.
            mount_server_provider(combiner, child, "child")
            mount_server_provider(combiner, hung, "hung")
        finally:
            toolcache_mod.UPSTREAM_TOOL_LIST_TIMEOUT = original
        # The aggregate list completes with the bounded provider skipped,
        # instead of hanging forever on the hung upstream.
        tools = await asyncio.wait_for(combiner.list_tools(), timeout=5.0)
        names = [t.name for t in tools]
        assert "child_ping" in names, names
        assert "hung_never_listed" not in names
        # The wrapper carried the patched bound and names its inner provider.
        hung_wrapped = combiner.providers[-1]
        assert "hung" in repr(hung_wrapped)
        assert cast(BoundedListProvider, getattr(hung_wrapped, "_inner"))._timeout == _LIST_TIMEOUT


# ── Single-flight owner bound + joiner release ─────────────────────


class TestFetchOwnerBound:
    """The single-flight owner cannot wedge a session (or its joiners) forever."""

    def setup_method(self) -> None:
        _reset_inflight_and_cache()

    def teardown_method(self) -> None:
        _restore_inflight_and_cache()

    async def test_owner_timeout_falls_back_and_releases(self) -> None:
        """Owner times out on a hung upstream → serves last-known-good, clears slot."""
        mw = srv.ToolProcessingMiddleware()
        gate = asyncio.Event()  # the owner's fetch hangs on this forever
        _hung_fetch._gate = gate  # type: ignore[attr-defined]

        task = asyncio.create_task(mw.on_list_tools(_context(), _hung_fetch))
        for _ in range(20):
            await asyncio.sleep(0)  # the owner reaches the hung fetch
        result = await asyncio.wait_for(task, timeout=5.0)
        # The fetch never completed within the bound: the owner falls back to
        # the (empty) last-known-good list instead of hanging forever, and the
        # slot is cleared so the next call can fetch again.
        assert result == []
        assert srv.ToolProcessingMiddleware._inflight is None

    async def test_owner_cancel_releases_joined_session(self) -> None:
        """An owner cancelled mid-fetch (client went away) releases its joiners."""
        mw = srv.ToolProcessingMiddleware()
        gate = asyncio.Event()
        _hung_fetch._gate = gate  # type: ignore[attr-defined]

        owner = asyncio.create_task(mw.on_list_tools(_context(), _hung_fetch))
        for _ in range(20):
            await asyncio.sleep(0)
        joiner = asyncio.create_task(mw.on_list_tools(_context(), _hung_fetch))
        for _ in range(20):
            await asyncio.sleep(0)  # the joiner piles onto the pending future
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(owner, timeout=2.0)
        # The joiner must not hang forever on the never-completing future.
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(joiner, timeout=2.0)
        assert srv.ToolProcessingMiddleware._inflight is None

    async def test_joiner_survives_pre_wedged_future_via_stale_cache(self) -> None:
        """The historical wedged shape: join onto a future nobody will resolve."""
        mw = srv.ToolProcessingMiddleware()

        wedged: asyncio.Future[list[Tool]] = asyncio.get_running_loop().create_future()
        srv.ToolProcessingMiddleware._inflight = wedged
        # A REAL Tool: the result still passes the egress passes (nvim append,
        # schema finalize), which touch tool attributes a mock would lack.
        stale_tool = Tool(
            name="stale_ping", parameters={"type": "object", "properties": {}, "required": []}
        )
        RUNTIME.tools.cache = [stale_tool]
        RUNTIME.tools.cache_time = 0  # older than CACHE_TTL → cache-miss path

        result = await asyncio.wait_for(mw.on_list_tools(_context(), _never_fetch), timeout=2.0)
        # Falls back to the last-known-good list instead of hanging, and the
        # dead future is dropped from the slot so a later fetch starts fresh.
        assert [t for t in result if t.name == "stale_ping"]
        assert srv.ToolProcessingMiddleware._inflight is not wedged

    async def test_joiner_raises_cleanly_when_no_stale_cache(self) -> None:
        """Same, but nothing last-known-good: a clean TimeoutError, not a hang."""
        mw = srv.ToolProcessingMiddleware()
        wedged: asyncio.Future[list[Tool]] = asyncio.get_running_loop().create_future()
        srv.ToolProcessingMiddleware._inflight = wedged

        with pytest.raises(TimeoutError):
            # The joiner's own bound (0.05s) fires well before this wrapper's.
            await asyncio.wait_for(mw.on_list_tools(_context(), _never_fetch), timeout=2.0)
