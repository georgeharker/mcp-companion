"""Tests for stdio process-per-chat isolation (``isolate: true`` — stdio).

The stdio analog of test_isolated.py's registry tiers: a chat's subprocess
IS its session, so grace expiry "parks" by leaving the child running (state
preserved in-process), the park-ttl forget terminates it, and a re-appearing
token reattaches to the still-running child.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest
from fastmcp.client.transports import StdioTransport

from mcp_combiner.isolated import (
    GRACE_SECONDS,
    PARK_TTL_SECONDS,
    REGISTRY,
    ParkedProc,
    ParkedSession,
    TokenKeyedStdioClient,
)


class FakeStdioClient:
    """Duck-typed per-chat stdio client: real StdioTransport (for the registry's
    isinstance dispatch) + close() mirroring the real teardown — the registry
    dispatches stdio teardown to ``close()`` (which in prod runs _disconnect
    AND transport.close, killing the keep_alive child)."""

    def __init__(self, command: str = "true") -> None:
        self.transport: Any = StdioTransport(command=command, args=[])
        self.initialize_result = None
        self.disconnected = False
        self.closed = False

    async def _disconnect(self, force: bool = False) -> None:
        self.disconnected = True

    async def close(self) -> None:
        self.closed = True
        await self._disconnect(force=True)


@pytest.fixture
def registry() -> Any:
    REGISTRY.reset()
    yield REGISTRY
    REGISTRY.reset()


# ── registry tiers ─────────────────────────────────────────────────


async def test_grace_expiry_parks_stdio_process_without_killing_it(registry: Any) -> None:
    client: Any = FakeStdioClient()
    entry = registry.register("srv", "tok", client, None)

    # Inside the grace window: untouched.
    assert await registry.sweep_once(now=entry.last_activity + registry.grace / 2) == 0
    assert not client.disconnected

    # Grace expiry: the subprocess is PARKED — still running (not disconnected).
    assert await registry.sweep_once(now=entry.last_activity + registry.grace + 1) == 1
    assert not client.disconnected
    assert ("srv", "tok") in registry.parked
    parked_entry = registry.parked[("srv", "tok")]
    assert isinstance(parked_entry, ParkedProc)
    assert parked_entry.client is client


async def test_ttl_expiry_forgets_stdio_process(registry: Any) -> None:
    client: Any = FakeStdioClient()
    entry = registry.register("srv", "tok", client, None)
    assert await registry.sweep_once(now=entry.last_activity + registry.grace + 1) == 1

    held: Any = registry.parked[("srv", "tok")]
    assert isinstance(held, ParkedProc)
    held.parked_at -= PARK_TTL_SECONDS + 60  # backdate past the ttl
    assert await registry.sweep_once() == 1
    # Forget terminates the child.
    assert client.disconnected
    assert ("srv", "tok") not in registry.parked


async def test_pop_parked_claims_for_reattach(registry: Any) -> None:
    client: Any = FakeStdioClient()
    registry.register("srv", "tok", client, None)
    await registry.sweep_once(now=entry_last(registry, "srv", "tok") + registry.grace + 1)

    claimed = registry.pop_parked("srv", "tok")
    assert isinstance(claimed, ParkedProc) and claimed.client is client
    assert ("srv", "tok") not in registry.parked  # claimed, not copied
    assert registry.pop_parked("srv", "tok") is None


def entry_last(registry: Any, server: str, token: str) -> float:
    entry: float = registry.live[(server, token)].last_activity
    return entry


async def test_evict_server_terminates_stdio_processes(registry: Any) -> None:
    live: Any = FakeStdioClient()
    parked: Any = FakeStdioClient()
    registry.register("srv", "tok-a", live, None)
    registry.register("srv", "tok-b", parked, None)
    await registry.sweep_once(
        now=registry.live[("srv", "tok-b")].last_activity + registry.grace + 1
    )
    assert isinstance(registry.parked.get(("srv", "tok-b")), ParkedProc)

    # A different server's entries must be untouched.
    other: Any = FakeStdioClient()
    registry.register("other", "tok-a", other, None)

    evicted = await registry.evict_server("srv")
    assert evicted == 2
    assert live.disconnected and parked.disconnected
    assert other.disconnected is False


async def test_close_all_terminates_stdio_even_on_handover_path(registry: Any) -> None:
    """stdio subprocesses cannot transfer to a successor combiner — unlike
    HTTP sessions (parked with terminate_on_close=False on the handover
    path), they are terminated in BOTH branches."""
    live: Any = FakeStdioClient()
    registry.register("srv", "tok", live, None)

    await registry.close_all(terminate=False)  # the sanctioned-handover path
    assert live.disconnected
    assert registry.live == {}


async def test_close_all_terminates_parked_stdio_processes(registry: Any) -> None:
    parked: Any = FakeStdioClient()
    registry.register("srv", "tok", parked, None)
    await registry.sweep_once(now=registry.live[("srv", "tok")].last_activity + GRACE_SECONDS + 1)

    await registry.close_all(terminate=False)
    assert parked.disconnected
    assert registry.parked == {}


# The HTTP tier must be unaffected by the stdio additions.
async def test_http_tiers_untouched_by_stdio_changes(registry: Any) -> None:
    from mcp_combiner.resume_transport import ResumableStreamableHttpTransport

    t = ResumableStreamableHttpTransport("http://127.0.0.1:1/mcp")
    t.resume_session_id = "sid-1234567890"
    t.resume_protocol_version = "2025-06-18"
    client: Any = FakeClientLike(transport=t)
    registry.register("srv", "tok", client, None)
    assert (
        await registry.sweep_once(
            now=registry.live[("srv", "tok")].last_activity + registry.grace + 1
        )
        == 1
    )
    # HTTP parks a ParkedSession into the ONE merged bucket.
    assert isinstance(registry.parked.get(("srv", "tok")), ParkedSession)


class FakeClientLike:
    """The HTTP-side fake from test_isolated.py (resumable-client shape)."""

    def __init__(self, transport: Any) -> None:
        self.transport = transport
        self.initialize_result = None
        self.disconnected = False

    async def _disconnect(self, force: bool = False) -> None:
        self.disconnected = True


# ── TokenKeyedStdioClient.acquire_stateful ──────────────────────────


@pytest.fixture
def client_factory(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A TokenKeyedStdioClient whose token resolution and context are stubbed;
    the global REGISTRY is reset around each test."""

    def _make(
        token_holder: dict[str, str | None],
        command: str = "true",
        args: list[str] | None = None,
    ) -> Any:
        import mcp_combiner.isolated as iso

        REGISTRY.reset()
        monkeypatch.setattr(iso, "_request_group_key", lambda: token_holder.get("token"))
        # get_context() outside a server session raises → session None; the
        # tokenless branch handles in-request vs out-of-context explicitly.

        return TokenKeyedStdioClient(server_name="srv", command=command, args=args or [])

    return _make


async def test_acquire_spawns_one_client_per_chat(client_factory: Any) -> None:
    holder: dict[str, str | None] = {"token": "tok-a"}
    stateful = client_factory(holder)

    a1 = await stateful.acquire_stateful()
    a2 = await stateful.acquire_stateful()
    assert a1 is a2  # same chat → same subprocess

    holder["token"] = "tok-b"
    b1 = await stateful.acquire_stateful()
    assert b1 is not a1  # different chat → different subprocess


async def test_acquire_reattaches_to_parked_process(client_factory: Any) -> None:
    import time

    holder: dict[str, str | None] = {"token": "tok-a"}
    stateful = client_factory(holder)

    first = await stateful.acquire_stateful()
    # Grace expire + park (registry-level; the client is left "running").
    assert await REGISTRY.sweep_once(now=time.monotonic() + GRACE_SECONDS + 1) == 1
    assert isinstance(REGISTRY.parked.get(("srv", "tok-a")), ParkedProc)

    again = await stateful.acquire_stateful()
    assert again is first  # reattached to the SAME still-running process
    assert ("srv", "tok-a") not in REGISTRY.parked  # claimed
    assert ("srv", "tok-a") in REGISTRY.live  # live again


async def test_tokenless_out_of_context_gets_ephemeral_probe(client_factory: Any) -> None:
    """Regression (round-5 B2): the startup/prime tools/list runs OUT of any
    request context — the old tokenless branch called ``new_stateful``, which
    needs a server context and raised, so an isolated stdio server NEVER
    primed (and every chat's tools/list leaked children). The fix: outside a
    request context, acquire returns an EPHEMERAL probe client
    (``keep_alive=False``) — the proxy lists through it in one context and the
    child dies with that context."""
    import asyncio
    import time

    holder: dict[str, str | None] = {"token": None}
    stateful = client_factory(holder, command=sys.executable, args=["-c", PID_SCRIPT])

    probe_client: Any = await stateful.acquire_stateful()
    async with probe_client:
        result = await probe_client.call_tool("pid", {})
        pid = _tool_int(result)
    assert pid > 0

    # The ephemeral child died with its context — no startup leak.
    deadline = time.monotonic() + 5
    while _pid_alive(pid) and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    assert not _pid_alive(pid)


async def test_tokenless_in_request_routes_to_inherited_machinery(
    client_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tokenless chat IN a request context rides the inherited per-ServerSession
    machinery and never touches the registry (supervised/unsupervised asymmetry).
    The parent's ``new_stateful`` is fastmcp's own code (sync; exercised in prod)
    — stub it and assert OUR routing."""
    from types import SimpleNamespace

    from fastmcp.server.providers.proxy import StatefulProxyClient

    import mcp_combiner.isolated as iso

    holder: dict[str, str | None] = {"token": None}
    stateful = client_factory(holder)
    monkeypatch.setattr(iso, "get_context", lambda: SimpleNamespace(session=None))

    sentinel = object()
    calls = {"n": 0}

    def _fake_new_stateful(self: Any) -> Any:  # new_stateful is sync in fastmcp
        calls["n"] += 1
        return sentinel

    monkeypatch.setattr(StatefulProxyClient, "new_stateful", _fake_new_stateful)

    base = await stateful.acquire_stateful()
    again = await stateful.acquire_stateful()
    assert base is sentinel and again is sentinel
    assert calls["n"] == 2
    assert REGISTRY.live == {}  # tokenless chats never touch the registry


async def test_stdio_children_killed_by_forget_evict_and_close_all(client_factory: Any) -> None:
    """Regression (round-5 B1): StdioTransport defaults keep_alive=True, so the
    registry's stdio teardown dispatches to ``close()`` (which runs the
    transport close). Assert with REAL subprocess PIDs that park+forget,
    evict_server and close_all each terminate the child — and that a token
    reattach inside the grace window reattaches to the SAME child."""
    import asyncio
    import time

    async def _wait_pid_gone(pid: int) -> None:
        deadline = time.monotonic() + 5
        while _pid_alive(pid) and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        assert not _pid_alive(pid), f"pid {pid} still alive"

    async def _child_pid(stateful: Any, holder: dict[str, str | None]) -> int:
        """Acquire and read the child's PID through its own `pid` tool.
        If the token is parked, this REATTACHES to the same child."""
        client: Any = await stateful.acquire_stateful()
        async with client:
            result = await client.call_tool("pid", {})
        return _tool_int(result)

    holder: dict[str, str | None] = {"token": "tok-a"}
    stateful = client_factory(holder, command=sys.executable, args=["-c", PID_SCRIPT])

    # Boot: one child.
    pid1 = await _child_pid(stateful, holder)

    # Park (grace) → reattach → SAME child still running (park semantics).
    await REGISTRY.sweep_once(now=time.monotonic() + GRACE_SECONDS + 1)
    assert isinstance(REGISTRY.parked.get(("srv", "tok-a")), ParkedProc)
    pid_reattach = await _child_pid(stateful, holder)
    assert pid_reattach == pid1
    assert ("srv", "tok-a") in REGISTRY.live  # live again after reattach

    # Repark (the reattach stamped last_activity), then ttl-forget.
    await REGISTRY.sweep_once(now=time.monotonic() + GRACE_SECONDS + 1)
    held: Any = REGISTRY.parked.get(("srv", "tok-a"))
    assert isinstance(held, ParkedProc)
    held.parked_at -= PARK_TTL_SECONDS + 60
    assert await REGISTRY.sweep_once() == 1
    await _wait_pid_gone(pid1)

    # evict_server terminates a parked child (park again first — the forget
    # in the previous phase already removed the entry).
    pid2 = await _child_pid(stateful, holder)
    await REGISTRY.sweep_once(now=time.monotonic() + GRACE_SECONDS + 1)
    assert isinstance(REGISTRY.parked.get(("srv", "tok-a")), ParkedProc)
    await REGISTRY.evict_server("srv")
    await _wait_pid_gone(pid2)

    # close_all(terminate=False) — the sanctioned-handover branch — still
    # terminates stdio children (they cannot transfer to a successor).
    pid3 = await _child_pid(stateful, holder)
    assert ("srv", "tok-a") in REGISTRY.live
    await REGISTRY.close_all(terminate=False)
    await _wait_pid_gone(pid3)


def _pid_alive(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover
        return True


def _tool_int(result: Any) -> int:
    return int(getattr(result, "data", result.content[0].text))


PID_SCRIPT = (
    "from fastmcp import FastMCP\n"
    "import os\n"
    "mcp = FastMCP('pidserver')\n"
    "@mcp.tool\n"
    "def pid() -> int:\n"
    "    return os.getpid()\n"
    "if __name__ == '__main__':\n"
    "    mcp.run()\n"
)


# ── real-subprocess e2e: two chats, independent in-memory state ──────

COUNTER_SCRIPT = (
    "from fastmcp import FastMCP\n"
    "mcp = FastMCP('counter')\n"
    "n = {'v': 0}\n"
    "\n"
    "@mcp.tool\n"
    "def bump() -> int:\n"
    "    n['v'] += 1\n"
    "    return n['v']\n"
    "\n"
    "if __name__ == '__main__':\n"
    "    mcp.run()\n"
)


async def test_two_chats_two_subprocesses_state_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gold test: chat A and chat B each get their OWN stateful stdio
    subprocess — bumps never cross. (Shared-subprocess isolation would make
    chat B see 2 here.)"""
    import mcp_combiner.isolated as iso

    REGISTRY.reset()
    tok = {"token": "tok-a"}
    stateful = TokenKeyedStdioClient(
        server_name="counter", command=sys.executable, args=["-c", COUNTER_SCRIPT]
    )
    monkeypatch.setattr(iso, "_request_group_key", lambda: tok["token"])
    try:
        client_a: Any = await stateful.acquire_stateful()
        async with client_a:
            r1 = await client_a.call_tool("bump", {})
            assert int(getattr(r1, "data", r1.content[0].text)) == 1

        tok["token"] = "tok-b"
        client_b: Any = await stateful.acquire_stateful()
        assert client_b is not client_a
        async with client_b:
            r = await client_b.call_tool("bump", {})
            assert int(getattr(r, "data", r.content[0].text)) == 1  # INDEPENDENT state
    finally:
        await REGISTRY.evict_server("counter")
        REGISTRY.reset()


STATE_PID_SCRIPT = (
    "from fastmcp import FastMCP\n"
    "import os\n"
    "mcp = FastMCP('counter')\n"
    "n = {'v': 0}\n"
    "@mcp.tool\n"
    "def bump() -> int:\n"
    "    n['v'] += 1\n"
    "    return n['v']\n"
    "@mcp.tool\n"
    "def pid() -> int:\n"
    "    return os.getpid()\n"
    "if __name__ == '__main__':\n"
    "    mcp.run()\n"
)


async def test_stdio_child_death_is_healed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression (round-6 B-2): a SIGKILLed per-chat child must be healed on
    the chat's NEXT call — the stateful clone's __aexit__ never reconnects, so
    before the fix the registry handed back the dead client forever and the
    chat stayed broken until a manual restart. After the fix: transparent
    respawn (bump restarts at 1, new pid)."""
    import asyncio
    import os
    import signal
    import time

    import mcp_combiner.isolated as iso

    REGISTRY.reset()
    tok = {"token": "tok-a"}
    stateful = TokenKeyedStdioClient(
        server_name="counter", command=sys.executable, args=["-c", STATE_PID_SCRIPT]
    )
    monkeypatch.setattr(iso, "_request_group_key", lambda: tok["token"])

    client_a: Any = await stateful.acquire_stateful()
    async with client_a:
        pid1 = _tool_int(await client_a.call_tool("pid", {}))
        assert _tool_int(await client_a.call_tool("bump", {})) == 1

    # Crash: SIGKILL the child.
    os.kill(pid1, signal.SIGKILL)
    deadline = time.monotonic() + 5
    while _pid_alive(pid1) and time.monotonic() < deadline:
        await asyncio.sleep(0.1)

    # Next acquire sees the dead child and heals: fresh subprocess, state gone.
    client_a2: Any = await stateful.acquire_stateful()
    assert client_a2 is not client_a
    async with client_a2:
        pid2 = _tool_int(await client_a2.call_tool("pid", {}))
        assert pid2 != pid1
        assert _tool_int(await client_a2.call_tool("bump", {})) == 1  # fresh state
    await REGISTRY.evict_server("counter")
    REGISTRY.reset()


async def test_concurrent_acquire_after_crash_spawns_one_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (round-7 B1): the crash heal used to await the dead child's
    close BETWEEN dropping the entry and registering the fresh one — two
    concurrent acquires on the same dead chat each spawned a child, and the
    first register overwrote the second, leaving an untracked child (alive
    after evict AND close_all). The heal now pops, spawns and registers
    synchronously; only the dead-child close awaits, AFTER registration, so
    a racing acquire finds the fresh live entry."""
    import asyncio
    import os
    import signal
    import time

    import mcp_combiner.isolated as iso

    REGISTRY.reset()
    tok = {"token": "tok-a"}
    stateful = TokenKeyedStdioClient(
        server_name="counter", command=sys.executable, args=["-c", STATE_PID_SCRIPT]
    )
    monkeypatch.setattr(iso, "_request_group_key", lambda: tok["token"])

    client_a: Any = await stateful.acquire_stateful()
    async with client_a:
        pid1 = _tool_int(await client_a.call_tool("pid", {}))

    os.kill(pid1, signal.SIGKILL)
    deadline = time.monotonic() + 5
    while _pid_alive(pid1) and time.monotonic() < deadline:
        await asyncio.sleep(0.1)

    # Two racing acquires for the same dead chat: BOTH must get the same
    # healed client (one child), and only the dead child must remain.
    healed_a, healed_b = await asyncio.gather(
        stateful.acquire_stateful(), stateful.acquire_stateful()
    )
    assert healed_a is healed_b

    # And the healed child serves the chat fresh.
    async with healed_a:
        pid2 = _tool_int(await healed_a.call_tool("pid", {}))
        assert pid2 != pid1
        assert _tool_int(await healed_a.call_tool("bump", {})) == 1

    # The dead child and the loser (if it ever existed) are GONE after evict.
    await REGISTRY.evict_server("counter")
    deadline = time.monotonic() + 5
    both_gone = f"{pid1}:{pid2}"
    deadline = time.monotonic() + 5
    while (_pid_alive(pid1) or _pid_alive(pid2)) and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    assert not _pid_alive(pid1) and not _pid_alive(pid2), both_gone
    REGISTRY.reset()


async def test_parked_child_death_heals_on_reattach(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression (round-7 gap #2): a parked per-chat child that DIES before its
    token reappears must heal at reattach — respawn fresh, not hand back a
    dead client (round-7's parked-dead branch, now synchronous like the live
    heal)."""
    import asyncio
    import os
    import signal
    import time

    import mcp_combiner.isolated as iso

    REGISTRY.reset()
    tok = {"token": "tok-a"}
    stateful = TokenKeyedStdioClient(
        server_name="counter", command=sys.executable, args=["-c", STATE_PID_SCRIPT]
    )
    monkeypatch.setattr(iso, "_request_group_key", lambda: tok["token"])

    client_a: Any = await stateful.acquire_stateful()
    async with client_a:
        pid1 = _tool_int(await client_a.call_tool("pid", {}))
        assert _tool_int(await client_a.call_tool("bump", {})) == 1

    # Park (grace expiry), THEN crash the parked child.
    await REGISTRY.sweep_once(now=time.monotonic() + GRACE_SECONDS + 1)
    assert isinstance(REGISTRY.parked.get(("counter", "tok-a")), ParkedProc)
    os.kill(pid1, signal.SIGKILL)
    deadline = time.monotonic() + 5
    while _pid_alive(pid1) and time.monotonic() < deadline:
        await asyncio.sleep(0.1)

    # Reattach: the dead parked child heals into a fresh one.
    healed: Any = await stateful.acquire_stateful()
    assert healed is not client_a
    async with healed:
        pid2 = _tool_int(await healed.call_tool("pid", {}))
        assert pid2 != pid1
        assert _tool_int(await healed.call_tool("bump", {})) == 1  # fresh state

    await REGISTRY.evict_server("counter")
    deadline = time.monotonic() + 5
    while (_pid_alive(pid1) or _pid_alive(pid2)) and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    assert not _pid_alive(pid1) and not _pid_alive(pid2)
    REGISTRY.reset()


async def test_heal_returns_without_awaiting_the_deferred_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-9 pin: the heal path must return promptly — the dead child's
    close runs as a TRACKED background task (no await between register and
    return; the await-window is where concurrent acquires raced and leaked
    untracked children). A slow ``_disconnect`` must not slow the acquire,
    the task must be visible in ``_expedite_tasks``, and ``close_all`` drains
    it to completion instead of cancelling mid-close."""
    import asyncio
    import os
    import signal
    import time

    import mcp_combiner.isolated as iso

    REGISTRY.reset()
    tok = {"token": "tok-a"}
    stateful = TokenKeyedStdioClient(
        server_name="counter", command=sys.executable, args=["-c", STATE_PID_SCRIPT]
    )
    monkeypatch.setattr(iso, "_request_group_key", lambda: tok["token"])

    client_a: Any = await stateful.acquire_stateful()
    async with client_a:
        pid1 = _tool_int(await client_a.call_tool("pid", {}))

    os.kill(pid1, signal.SIGKILL)
    deadline = time.monotonic() + 5
    while _pid_alive(pid1) and time.monotonic() < deadline:
        await asyncio.sleep(0.1)

    close_calls: list[Any] = []
    finished: list[int] = []
    real_disconnect = iso.REGISTRY._disconnect

    async def _slow_disconnect(entry: Any, key: Any) -> None:
        close_calls.append(key)
        if entry.client is client_a:
            # Slow ONLY the dead child's close — close_all's own sequential
            # disconnect of the healed (live) entry must stay instant, so the
            # drain assertion below can't pass by coincidence.
            await asyncio.sleep(1.0)
        await real_disconnect(entry, key)
        if entry.client is client_a:
            finished.append(1)  # completed AFTER real_disconnect — marks a REAL drain

    monkeypatch.setattr(iso.REGISTRY, "_disconnect", _slow_disconnect)

    started = time.monotonic()
    healed: Any = await stateful.acquire_stateful()
    assert healed.transport is not client_a.transport  # fresh PRIVATE child
    elapsed = time.monotonic() - started
    assert elapsed < 0.5, elapsed  # the 1s slow close did NOT block the heal
    assert REGISTRY._dead_close_tasks, "the close task must be tracked"
    # The heal path has no awaits, so the deferred task starts on the NEXT
    # loop tick — yield one before asserting its side effects.
    await asyncio.sleep(0.01)
    assert close_calls, "the deferred close task must have been spawned"

    # close_all DRAINS the tracked task: the slow close runs to completion
    # during shutdown rather than being cancelled mid-close (a cancelled
    # close could leave a session-dead-but-maybe-alive child undisposed).
    started = time.monotonic()
    await REGISTRY.close_all(terminate=False)
    drained = time.monotonic() - started
    assert drained >= 0.9, drained  # waited out the 1s slow close
    assert finished == [1], (
        finished,
        drained,
    )  # the close RAN TO COMPLETION — drained, not cancelled
    assert REGISTRY._expedite_tasks == set()  # done-callbacks cleaned up
    assert REGISTRY._dead_close_tasks == set()  # drained too
    # And the crash-healed child itself was terminated by close_all.
    await REGISTRY.evict_server("counter")
    REGISTRY.reset()
