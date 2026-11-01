"""Tests for externalServers (non-owned backing processes).

The lifecycle flavors map one-to-one onto tests:
- launch + leave running (no ``stop``, not foreground)
- launch + explicit ``stop`` command
- launch + ``foreground: true`` (child owned + SIGTERMed)
- attach-only (no ``launch``; poll-only), including dangling-reference warnings
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from mcp_combiner.backing import BackingProcessManager
from mcp_combiner.config import CombinerConfig, ExternalServerConfig
from mcp_combiner.externalservers import ExternalServerManager


def _mock_async_process(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> AsyncMock:
    """Async-mock the create_subprocess_exec factory, mirroring test_sharedserver.

    ``wait`` is set (not just ``communicate``) because detached external
    launches await ``proc.wait()`` rather than ``communicate()``.
    """
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.wait = AsyncMock(return_value=returncode)
    factory = AsyncMock(return_value=proc)
    return factory


# ── config parsing ─────────────────────────────────────────────────


def _make_config(
    *,
    external: dict[str, Any] | None = None,
    servers: dict[str, Any] | None = None,
    shared_servers: dict[str, Any] | None = None,
) -> CombinerConfig:
    ext = external or {
        "launch": ["docker", "run", "-d", "-p", "8002:8000", "mcp/server"],
        "stop": ["docker", "stop", "mcp-server"],
        "health_timeout": 1,
    }
    raw = {
        "externalServers": {"postgres_mcp": ext} if ext else {},
        "servers": servers
        or {"postgres": {"url": "http://localhost:8002/mcp", "externalServer": "postgres_mcp"}},
    }
    if shared_servers:
        raw["sharedServers"] = shared_servers
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(raw, f)
        path = f.name
    config = CombinerConfig.load(path)
    os.unlink(path)
    return config


def test_external_config_parses() -> None:
    config = _make_config()
    ext = config.external_servers["postgres_mcp"]
    assert ext.launch == ["docker", "run", "-d", "-p", "8002:8000", "mcp/server"]
    assert ext.stop == ["docker", "stop", "mcp-server"]
    assert ext.foreground is False
    assert ext.health_timeout == 1


def test_external_config_attach_only() -> None:
    config = _make_config(external={"health_timeout": 5})
    ext = config.external_servers["postgres_mcp"]
    assert ext.launch is None
    assert ext.stop is None


def test_external_config_grace_period_warned_and_ignored(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Ownership semantics don't apply to non-owned entries — warned, ignored."""
    with caplog.at_level(logging.WARNING):
        config = _make_config(external={"grace_period": "30m", "command": "uvx"})
    assert any("grace_period" in r.message for r in caplog.records)
    assert any("unknown config key" in r.message for r in caplog.records)
    # Warn + ignore: the entry still loads, just without sharedserver semantics.
    assert "postgres_mcp" in config.external_servers


def test_server_entry_external_field() -> None:
    config = _make_config()
    srv = config.servers["postgres"]
    assert srv.external_server == "postgres_mcp"
    assert config.resolve_external_server("postgres") is not None


def test_backing_claimant_shared_server_wins_when_both_set() -> None:
    """Precedence is the facade's manager ORDER — first claimant wins.

    ``resolve_external_server`` resolves the server's own reference even when
    ``sharedServer`` is also set; ``BackingProcessManager.claimant`` is where
    "sharedServer wins" is decided (and the load config warns the author).
    """
    config = _make_config(
        external={"health_timeout": 1},
        servers={
            "postgres": {
                "url": "http://localhost:8002/mcp",
                "externalServer": "postgres_mcp",
                "sharedServer": "goog_ws",
            }
        },
        shared_servers={"goog_ws": {"command": "uvx", "args": ["a"], "health_timeout": 1}},
    )
    facade = BackingProcessManager(config)
    assert facade.claimant("postgres") is facade.shared


def test_backing_claimant_dangling_shared_falls_through_to_external() -> None:
    """A dangling sharedServer reference must not strand the server: the
    external manager still owns it (pre-review, this fell through to the
    probe, whose ensure_started is a no-op — the backend never launched)."""
    config = _make_config(
        servers={
            "postgres": {
                "url": "http://localhost:8002/mcp",
                "externalServer": "postgres_mcp",
                "sharedServer": "missing",
            }
        }
    )
    facade = BackingProcessManager(config)
    assert facade.external is not None
    assert facade.claimant("postgres") is facade.external


# ── ExternalServerManager ──────────────────────────────────────────


@pytest.mark.anyio
async def test_start_all_runs_launch_command() -> None:
    config = _make_config()
    mgr = ExternalServerManager(config)

    mock_exec = _mock_async_process(returncode=0)
    with (
        patch("asyncio.create_subprocess_exec", mock_exec),
        patch("mcp_combiner.externalservers._poll_url", new=AsyncMock(return_value=True)),
    ):
        await mgr.start_all()

    # call_args_list entries are (args_tuple, kwargs) — argv is args_tuple[0:].
    assert list(mock_exec.call_args_list[0][0])[:4] == ["docker", "run", "-d", "-p"]
    assert "postgres_mcp" in mgr._active
    assert mgr.backing_state("postgres") == "reachable"


@pytest.mark.anyio
async def test_start_all_launch_failure_records_state() -> None:
    config = _make_config()
    mgr = ExternalServerManager(config)

    mock_exec = _mock_async_process(returncode=1)
    with patch("asyncio.create_subprocess_exec", mock_exec):
        await mgr.start_all()

    assert "postgres_mcp" not in mgr._active
    assert mgr.backing_state("postgres") == "spawn_failed"


@pytest.mark.anyio
async def test_start_all_attach_only_polls_without_launching() -> None:
    config = _make_config(external={"health_timeout": 1})
    mgr = ExternalServerManager(config)

    mock_exec = _mock_async_process()
    with (
        patch("asyncio.create_subprocess_exec", mock_exec),
        patch("mcp_combiner.externalservers._poll_url", new=AsyncMock(return_value=True)),
    ):
        await mgr.start_all()

    mock_exec.assert_not_called()
    assert mgr.backing_state("postgres") == "reachable"


@pytest.mark.anyio
async def test_start_all_no_url_marks_spawned() -> None:
    config = _make_config(
        servers={
            "postgres": {
                "url": None,
                "transport": "stdio",
                "command": "echo",
                "externalServer": "postgres_mcp",
            }
        }
    )
    mgr = ExternalServerManager(config)
    with patch("asyncio.create_subprocess_exec", _mock_async_process(returncode=0)):
        await mgr.start_all()
    assert mgr.backing_state("postgres") == "spawned"


@pytest.mark.anyio
async def test_stop_all_runs_stop_command() -> None:
    config = _make_config()
    mgr = ExternalServerManager(config)
    mgr._active.append("postgres_mcp")

    mock_exec = _mock_async_process(returncode=0)
    with patch("asyncio.create_subprocess_exec", mock_exec):
        await mgr.stop_all()

    assert list(mock_exec.call_args_list[0][0]) == ["docker", "stop", "mcp-server"]
    assert "postgres_mcp" not in mgr._active
    assert mgr.backing_state("postgres") == "stopped"


@pytest.mark.anyio
async def test_stop_all_leaves_running_without_stop_config() -> None:
    """Non-owned: no `stop` configured → the process outlives the combiner."""
    config = _make_config(external={"launch": ["docker", "run", "-d", "img"]})
    mgr = ExternalServerManager(config)
    mgr._active.append("postgres_mcp")

    mock_exec = _mock_async_process()
    with patch("asyncio.create_subprocess_exec", mock_exec):
        await mgr.stop_all()

    mock_exec.assert_not_called()


@pytest.mark.anyio
async def test_restart_stops_then_relaunchs() -> None:
    config = _make_config()
    mgr = ExternalServerManager(config)
    mgr._active.append("postgres_mcp")

    mock_exec = _mock_async_process(returncode=0)
    with (
        patch("asyncio.create_subprocess_exec", mock_exec),
        patch("mcp_combiner.externalservers._poll_url", new=AsyncMock(return_value=True)),
    ):
        assert await mgr.restart("postgres") is True
        calls = mock_exec.call_args_list

    # First call: stop; second: relaunch (argv is each entry's args_tuple).
    assert list(calls[0][0])[:2] == ["docker", "stop"]
    assert list(calls[1][0])[:2] == ["docker", "run"]
    assert mgr.backing_state("postgres") == "reachable"


@pytest.mark.anyio
async def test_restart_attach_only_returns_false() -> None:
    config = _make_config(external={"health_timeout": 1})
    mgr = ExternalServerManager(config)
    assert await mgr.restart("postgres") is False


# ── review regressions (B1: restart bookkeeping; B4: attach-only stop) ──


@pytest.mark.anyio
async def test_restart_replaces_active_entry_not_duplicated() -> None:
    """Regression: restart must not leave a duplicate in ``_active``.

    A duplicate made the next disable a no-op (name still "active") and the
    next boot never relaunch the backend — and sharedserver's ``unuse`` would
    decrement one reference too many per restart.
    """
    config = _make_config()
    mgr = ExternalServerManager(config)

    mock_exec = _mock_async_process(returncode=0)
    with (
        patch("asyncio.create_subprocess_exec", mock_exec),
        patch("mcp_combiner.externalservers._poll_url", new=AsyncMock(return_value=True)),
    ):
        await mgr.start_all()
        assert mgr._active == ["postgres_mcp"]

        assert await mgr.restart("postgres") is True
        assert mgr._active.count("postgres_mcp") == 1  # replaced, not duplicated

        await mgr.ensure_stopped("postgres")
        assert mgr._active == []

        # Re-enable after the restart+disable cycle relaunches (would have
        # been a no-op with the duplicate still present at count 2 → 1).
        calls_before = len(mock_exec.call_args_list)
        await mgr.ensure_started("postgres")
        assert len(mock_exec.call_args_list) == calls_before + 1
        assert mgr._active == ["postgres_mcp"]


@pytest.mark.anyio
async def test_shared_restart_replaces_active_entry_not_duplicated() -> None:
    """Same regression for the shared kind: restart is replace-not-append
    (sharedserver ``unuse`` decrements once per held reference)."""
    from mcp_combiner.sharedserver import SharedServerManager

    config = _make_config(
        servers={
            "postgres": {
                "url": "http://localhost:8002/mcp",
                "sharedServer": "goog_ws",
            }
        },
        shared_servers={"goog_ws": {"command": "uvx", "args": ["a"], "health_timeout": 1}},
    )
    mgr = SharedServerManager(config)

    mock_exec = _mock_async_process(returncode=0)
    with (
        patch("shutil.which", return_value="/usr/bin/sharedserver"),
        patch("asyncio.create_subprocess_exec", mock_exec),
        patch("mcp_combiner.sharedserver._poll_url", new=AsyncMock(return_value=True)),
    ):
        await mgr.start_all()
        assert mgr._active == ["goog_ws"]

        assert await mgr.restart("postgres") is True
        assert mgr._active.count("goog_ws") == 1

        await mgr.ensure_stopped("postgres")
        assert mgr._active == []

        calls_before = len(mock_exec.call_args_list)
        await mgr.ensure_started("postgres")
        assert len(mock_exec.call_args_list) == calls_before + 1


@pytest.mark.anyio
async def test_attach_only_never_runs_stop() -> None:
    """Regression: attach-only entries don't own the process, so a configured
    ``stop`` must never run (the config loader warns about the combination).
    """
    config = _make_config(external={"stop": ["docker", "stop", "x"], "health_timeout": 1})
    mgr = ExternalServerManager(config)

    mock_exec = _mock_async_process()
    # The poll is mocked too — _poll_url itself shells out to curl and would
    # otherwise consume the same exec mock, muddying the assertion.
    with (
        patch("asyncio.create_subprocess_exec", mock_exec),
        patch("mcp_combiner.externalservers._poll_url", new=AsyncMock(return_value=True)),
    ):
        await mgr.start_all()
        await mgr.ensure_stopped("postgres")
        await mgr.stop_all()
    mock_exec.assert_not_called()  # no stop command was ever executed


@pytest.mark.anyio
async def test_foreground_chatty_child_does_not_deadlock() -> None:
    """Regression: foreground drain must read both pipes concurrently.

    A sequential stdout-then-stderr reader deadlocks once the child fills the
    stderr pipe buffer while stdout hasn't EOFed (the child blocks writing
    stderr, so stdout never closes). Uses a REAL child subprocess writing
    ~200KB to each stream — the old shape hangs; per-pipe tasks finish.
    """
    import asyncio
    import sys

    # One line LONGER than the 64KiB StreamReader limit (the old
    # `async for line` drain died with ValueError on exactly this), then
    # many short lines, then stdout, then exit.
    script = (
        "import sys\n"
        "sys.stderr.write('e' * 100000 + '\\n')\n"
        "for _ in range(5000): sys.stderr.write('y' * 32 + '\\n')\n"
        "sys.stdout.write('o' * 200000)\n"
    )
    config = _make_config(
        external={
            "launch": [sys.executable, "-c", script],
            "foreground": True,
            "health_timeout": 1,
        }
    )
    mgr = ExternalServerManager(config)
    with patch("mcp_combiner.externalservers._poll_url", new=AsyncMock(return_value=False)):
        await mgr.start_all()  # must not wedge on the drain

    assert mgr.backing_state("postgres") == "unreachable"
    # The child exited cleanly (a dead drain task would wedge it), and the
    # drain tasks finished WITHOUT exceptions — no return_exceptions escape
    # hatch here: a ValueError in the drain must fail this test.
    child = mgr._children["postgres_mcp"]
    await asyncio.wait_for(child.wait(), timeout=10)
    assert child.returncode == 0
    tasks = mgr._drain_tasks["postgres_mcp"]
    results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
    assert all(r is None for r in results)


# ── second-review regressions (double-launch on both-set; disable-vs-launch;
#    shutdown snapshots; restartable narrowing) ────────────────────────────


@pytest.mark.anyio
async def test_start_all_precedence_no_double_launch() -> None:
    """Regression: a server setting BOTH refs must launch ONE backend.

    Precedence used to exist only in ``claimant()`` dispatch, so each
    manager's ``start_all`` self-enumerated and the both-set server launched
    twice (``sharedserver use`` AND ``docker run``). The facade now groups by
    ``claimant()`` and hands each manager exactly its own servers.
    """
    config = _make_config(
        external={"launch": ["docker", "run", "-d", "img"], "health_timeout": 1},
        servers={
            "postgres": {
                "url": "http://localhost:8002/mcp",
                "externalServer": "postgres_mcp",
                "sharedServer": "goog_ws",
            }
        },
        shared_servers={"goog_ws": {"command": "uvx", "args": ["a"], "health_timeout": 1}},
    )
    facade = BackingProcessManager(config)
    mock_exec = _mock_async_process(returncode=0)
    with (
        patch("shutil.which", return_value="/usr/bin/sharedserver"),
        patch("asyncio.create_subprocess_exec", mock_exec),
        patch("mcp_combiner.sharedserver._poll_url", new=AsyncMock(return_value=True)),
        patch("mcp_combiner.externalservers._poll_url", new=AsyncMock(return_value=True)),
    ):
        await facade.start_all()

    calls = [list(c[0]) for c in mock_exec.call_args_list]
    assert any(c[:3] == ["/usr/bin/sharedserver", "use", "goog_ws"] for c in calls)
    assert not any(c[0] == "docker" for c in calls), calls
    assert facade.external is not None
    assert facade.external._active == []


@pytest.mark.anyio
async def test_disable_during_launch_stops_after_launch() -> None:
    """Regression: a disable racing a slow launch must not leave a running
    backend behind a "disabled" server (a detached launch can take 30s)."""
    import asyncio

    class _Slow(ExternalServerManager):
        # Signature matches the base hook — poll intentionally skipped.
        async def _launch_and_poll(self, server_name: str, entry: Any, url: str | None) -> None:
            await asyncio.sleep(0.15)
            self._active.append(entry.name)  # _poll skipped — not under test

    config = _make_config()
    mgr = _Slow(config)
    mock_exec = _mock_async_process(returncode=0)
    with (
        patch("asyncio.create_subprocess_exec", mock_exec),
        patch("mcp_combiner.externalservers._poll_url", new=AsyncMock(return_value=True)),
    ):
        task = asyncio.ensure_future(mgr.ensure_started("postgres"))
        await asyncio.sleep(0.05)  # launch now in flight
        await mgr.ensure_stopped("postgres")  # must defer, not act mid-launch
        assert "postgres_mcp" in mgr._stop_after_launch

        await asyncio.wait_for(task, timeout=5)
        # The deferred stop ran once the launch completed: stop-cmd executed,
        # bookkeeping cleared, state recorded.
        calls = [list(c[0]) for c in mock_exec.call_args_list]
        assert ["docker", "stop", "mcp-server"] in calls
        assert mgr._active == []
        assert mgr.backing_state("postgres") == "stopped"

    @pytest.mark.anyio
    async def test_shutdown_uses_launch_time_snapshot() -> None:
        """Shutdown stops the process with the entry AS IT WAS AT LAUNCH —
        a reload that rewrites/drops the entry (or the live config) cannot
        change what a running container gets told at shutdown."""
        config = _make_config()
        mgr = ExternalServerManager(config)

        with (
            patch("asyncio.create_subprocess_exec", _mock_async_process(returncode=0)),
            patch("mcp_combiner.externalservers._poll_url", new=AsyncMock(return_value=True)),
        ):
            await mgr.start_all()

        # Simulate reload_config's in-place swap: rewrite (or would-be-drop)
        # the live entry after launch.
        config.external_servers["postgres_mcp"].stop = ["docker", "rm", "-f", "WRONG"]

        mock_exec = _mock_async_process(returncode=0)
        with patch("asyncio.create_subprocess_exec", mock_exec):
            await mgr.stop_all()

        calls = [list(c[0]) for c in mock_exec.call_args_list]
        assert ["docker", "stop", "mcp-server"] in calls
        assert not any("WRONG" in str(c) for c in calls)


def test_detached_launch_without_stop_is_not_restartable() -> None:
    """A detached launch with no ``stop`` can't be safely restarted: re-running
    the launch while the old process lives fails (`--name` taken) and would
    ignore the config's leave-running posture. Disable/restart fall back to
    the caller's reconnect path instead."""
    config = _make_config(external={"launch": ["docker", "run", "-d", "img"], "health_timeout": 1})
    mgr = ExternalServerManager(config)
    entry = config.external_servers["postgres_mcp"]
    assert mgr.restartable(entry) is False
    with_stop = _make_config(
        external={"launch": ["docker", "run", "-d", "img"], "stop": ["docker", "stop", "x"]}
    )
    assert mgr.restartable(with_stop.external_servers["postgres_mcp"]) is True
    foreground = _make_config(external={"launch": ["docker", "run", "img"], "foreground": True})
    assert mgr.restartable(foreground.external_servers["postgres_mcp"]) is True
    attach = _make_config(external={"health_timeout": 1})
    assert mgr.restartable(attach.external_servers["postgres_mcp"]) is False


@pytest.mark.anyio
async def test_enable_after_disable_racing_launch_cancels_stop_intent() -> None:
    """Regression: disable then ENABLE while an entry's launch is in flight —
    the LAST request wins. The launch completing must NOT stop the backend
    (round-3 review: the enable was silently eating the disable's stop intent
    into a stopped-but-enabled server; the fix discards the intent)."""
    import asyncio

    class _Slow(ExternalServerManager):
        async def _launch_and_poll(self, server_name: str, entry: Any, url: str | None) -> None:
            await asyncio.sleep(0.15)
            self._active.append(entry.name)
            await self._poll(server_name, entry, url)  # mocked — records state

    config = _make_config()
    mgr = _Slow(config)
    mock_exec = _mock_async_process(returncode=0)
    with (
        patch("asyncio.create_subprocess_exec", mock_exec),
        patch("mcp_combiner.externalservers._poll_url", new=AsyncMock(return_value=True)),
    ):
        task = asyncio.ensure_future(mgr.ensure_started("postgres"))
        await asyncio.sleep(0.05)  # launch in flight
        await mgr.ensure_stopped("postgres")  # records stop-after-launch intent
        assert "postgres_mcp" in mgr._stop_after_launch
        await mgr.ensure_started("postgres")  # enable wins → intent cancelled
        assert "postgres_mcp" not in mgr._stop_after_launch

        await asyncio.wait_for(task, timeout=5)
        calls = [list(c[0]) for c in mock_exec.call_args_list]
        assert not any(c[:2] == ["docker", "stop"] for c in calls)
        assert mgr._active == ["postgres_mcp"]
        assert mgr.backing_state("postgres") == "reachable"


@pytest.mark.anyio
async def test_restart_honours_disable_racing_it() -> None:
    """Regression: a disable racing a restart must not relaunch. Restart's
    stop-hard destroys the process; the disable's last-wins intent means the
    relaunch is skipped (and combiner__restart_server skips the remount)."""
    import asyncio

    class _SlowStop(ExternalServerManager):
        async def _stop_hard(self, entry: ExternalServerConfig) -> None:
            await asyncio.sleep(0.15)
            await super()._stop_hard(entry)

    config = _make_config()
    mgr = _SlowStop(config)
    mock_exec = _mock_async_process(returncode=0)
    with (
        patch("asyncio.create_subprocess_exec", mock_exec),
        patch("mcp_combiner.externalservers._poll_url", new=AsyncMock(return_value=True)),
    ):
        await mgr.start_all()

        task = asyncio.ensure_future(mgr.restart("postgres"))
        await asyncio.sleep(0.05)  # restart in flight (stop-hard sleeping)
        await mgr.ensure_stopped("postgres")  # disable wins after the hard stop
        assert await task is False  # NO relaunch

        calls = [list(c[0]) for c in mock_exec.call_args_list]
        assert calls[0][:2] == ["docker", "run"]  # boot launch
        assert calls[1][:2] == ["docker", "stop"]  # restart's hard stop
        assert not any(c[:2] == ["docker", "run"] for c in calls[2:]), calls
        assert mgr._active == []
        assert mgr.backing_state("postgres") == "stopped"


@pytest.mark.anyio
async def test_probe_done_callback_does_not_drop_newer_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the probe's done-callback popped tasks BY NAME — a
    disable→enable race meant the OLD cancelled probe's callback removed the
    NEW probe from ``_tasks``, orphaning it (never cancellable, invisible to
    stop_all). The callback now matches on task identity."""
    import asyncio

    config = _make_mixed_config()
    config.servers["shared_backed"].shared_server = None
    config.servers["ext_backed"].external_server = None
    mgr = BackingProcessManager(config)
    _mock_both_polls(monkeypatch, ready=True)
    probe = mgr.probe
    assert probe is not None

    async def _slow_poll(url: object, timeout: int) -> bool:
        await asyncio.sleep(0.1)
        return True

    monkeypatch.setattr("mcp_combiner.sharedserver._poll_url", _slow_poll)

    await mgr.ensure_started("shared_backed")  # probe A (slow)
    assert "shared_backed" in probe._tasks
    await mgr.ensure_stopped("shared_backed")  # cancels A
    await mgr.ensure_started("shared_backed")  # probe B (fresh)
    task_b = probe._tasks.get("shared_backed")
    assert task_b is not None and not task_b.done()

    # A's cancellation callback fires on the next loop tick; with the bug it
    # popped B. The identity check must keep B alive and running.
    await asyncio.sleep(0.02)
    assert probe._tasks.get("shared_backed") is task_b
    await mgr.stop_all()  # cleanup — B is cancellable via _tasks


# ── third-review round: probe staleness, launch timeouts, stop budgets ───


@pytest.mark.anyio
async def test_probe_reruns_after_disable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: a re-enabled plain url-only server gets a FRESH probe —
    the stale ``stopped`` verdict must not linger (or worse, mask a server
    that came back up while it was disabled)."""
    import asyncio

    config = _make_mixed_config()
    config.servers["shared_backed"].shared_server = None
    config.servers["ext_backed"].external_server = None
    mgr = BackingProcessManager(config)
    _mock_both_polls(monkeypatch, ready=True)
    monkeypatch.setattr(
        "asyncio.create_subprocess_exec",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no launches")),
    )

    async def _settle() -> None:
        probe = mgr.probe
        assert probe is not None
        await asyncio.gather(*list(probe._tasks.values()), return_exceptions=True)

    with patch("asyncio.create_subprocess_exec", _mock_async_process(returncode=0)):
        await mgr.start_all()
        await _settle()
        assert mgr.backing_state("shared_backed") == "reachable"

        await mgr.ensure_stopped("shared_backed")
        assert mgr.backing_state("shared_backed") == "stopped"

        # Re-enable: a fresh probe task replaces the cancelled one...
        await mgr.ensure_started("shared_backed")
        probe = mgr.probe
        assert probe is not None
        assert "shared_backed" in probe._tasks
        await _settle()
        # ...and the verdict is live again, not the stale "stopped".
        assert mgr.backing_state("shared_backed") == "reachable"


@pytest.mark.anyio
async def test_detached_launch_timeout_kills_launcher() -> None:
    """A hung detached launcher is killed and reaped, state is spawn_failed,
    and nothing is added to ``_active`` (nothing to stop later)."""
    import asyncio

    config = _make_config(external={"launch": ["docker", "run", "-d", "img"], "health_timeout": 1})
    mgr = ExternalServerManager(config)

    proc = AsyncMock()
    proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
    factory = AsyncMock(return_value=proc)
    with (
        patch("asyncio.create_subprocess_exec", factory),
        patch("mcp_combiner.externalservers._poll_url", new=AsyncMock(return_value=True)),
    ):
        await mgr.start_all()

    assert proc.kill.called
    assert proc.wait.called
    assert mgr.backing_state("postgres") == "spawn_failed"
    assert mgr._active == []


@pytest.mark.anyio
async def test_stop_budget_shared_across_stop_cmd_and_child() -> None:
    """Regression: the stop command and the child termination SHARE the
    budget — a hung stop command is killed at the deadline instead of eating
    the whole window and blowing sharedserver's ~5s SIGTERM→SIGKILL bound.
    Uses a REAL hung child (a sleeping interpreter) so the kill/reap paths
    are exercised for real."""
    import sys
    import time

    hang = [sys.executable, "-c", "import time; time.sleep(30)"]
    config = _make_config(
        external={"launch": hang, "stop": hang, "foreground": True, "health_timeout": 1}
    )
    mgr = ExternalServerManager(config)
    mgr.stop_timeout = 0.4  # instance override — keep the test fast
    with patch("mcp_combiner.externalservers._poll_url", new=AsyncMock(return_value=False)):
        await mgr.start_all()

    assert mgr.backing_state("postgres") == "unreachable"

    started = time.monotonic()
    await mgr.ensure_stopped("postgres")  # REAL: hangs in the stop cmd + child
    elapsed = time.monotonic() - started

    # Old behaviour: 0.4s stop wait + 5s terminate wait ≈ 5.4s. New: the kill
    # fires at the budget and the reap is immediate — well under 3s total.
    assert elapsed < 3.0, elapsed
    assert "postgres_mcp" not in mgr._children  # _stop popped and reaped it


# ── BackingProcessManager facade ───────────────────────────────────


def _make_mixed_config() -> CombinerConfig:
    """Two servers: one sharedserver-backed, one external-backed, one plain."""
    raw: dict[str, Any] = {
        "sharedServers": {"goog_ws": {"command": "uvx", "args": ["a"], "health_timeout": 1}},
        "externalServers": {
            "ext": {
                "launch": ["docker", "run", "-d", "img"],
                "stop": ["docker", "stop", "ext"],
                "health_timeout": 1,
            }
        },
        "servers": {
            "shared_backed": {"url": "http://localhost:8011/mcp", "sharedServer": "goog_ws"},
            "ext_backed": {"url": "http://localhost:8012/mcp", "externalServer": "ext"},
        },
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(raw, f)
        path = f.name
    config = CombinerConfig.load(path)
    os.unlink(path)
    return config


def _mock_both_polls(
    monkeypatch: pytest.MonkeyPatch, ready: bool = True, which: str | None = None
) -> None:
    """Patch both managers' URL polls and the sharedserver binary discovery.

    ``which`` defaults to ``None`` so a dispatch mistake can never reach the
    real sharedserver binary from a unit test; pass a fake path when the test
    patches ``create_subprocess_exec`` and wants the shared path to run.
    """
    monkeypatch.setattr("shutil.which", lambda *_a, **_k: which)
    monkeypatch.setattr("mcp_combiner.sharedserver._poll_url", AsyncMock(return_value=ready))
    monkeypatch.setattr("mcp_combiner.externalservers._poll_url", AsyncMock(return_value=ready))


@pytest.mark.anyio
async def test_facade_dispatches_by_backing_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _make_mixed_config()
    facade = BackingProcessManager(config)
    # create_subprocess_exec is mocked below, so a fake binary path is safe —
    # and needed for the shared half of the dispatch to exercise at all.
    _mock_both_polls(monkeypatch, ready=True, which="/usr/bin/sharedserver")

    mock_exec = _mock_async_process(returncode=0)
    with patch("asyncio.create_subprocess_exec", mock_exec):
        await facade.start_all()

    # Each kind dispatched to its own manager: shared went through
    # SharedServerManager, external got the raw launch argv, and the plain
    # url-only case — none here — would get a background probe only.
    assert "goog_ws" in facade.shared._active
    assert facade.external is not None
    assert facade.external is not None
    assert "ext" in facade.external._active
    assert facade.backing_state("shared_backed") == "reachable"
    assert facade.backing_state("ext_backed") == "reachable"


@pytest.mark.anyio
async def test_facade_external_restart_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _make_mixed_config()
    config.servers["shared_backed"].shared_server = None  # make it plain url-only
    mgr = BackingProcessManager(config)
    assert mgr.external is not None
    mgr.external._active.append("ext")
    _mock_both_polls(monkeypatch, ready=True)

    mock_exec = _mock_async_process(returncode=0)
    with patch("asyncio.create_subprocess_exec", mock_exec):
        # External-backed: stop the configured way, then relaunch.
        assert await mgr.restart("ext_backed") is True
        calls = [list(c[0]) for c in mock_exec.call_args_list]
        assert calls[0][:2] == ["docker", "stop"]
        assert calls[1][:2] == ["docker", "run"]
        assert mgr.backing_state("ext_backed") == "reachable"

        # And a restartable=False external (detach-without-stop) falls back
        # to the caller's reconnect path via dispatch, launching nothing.
        config.servers["ext_backed"] = config.servers["ext_backed"].model_copy(
            update={"external_server": "no_stop"}
        )
        config.external_servers["no_stop"] = config.external_servers["ext"].model_copy(
            update={"stop": None}
        )
        before = len(mock_exec.call_args_list)
        assert await mgr.restart("ext_backed") is False
        assert len(mock_exec.call_args_list) == before


@pytest.mark.anyio
async def test_facade_plain_restart_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _make_mixed_config()
    config.servers["shared_backed"].shared_server = None
    config.servers["ext_backed"].external_server = None
    mgr = BackingProcessManager(config)
    _mock_both_polls(monkeypatch)

    with patch("asyncio.create_subprocess_exec", _mock_async_process(returncode=0)):
        # Plain url-only: nothing to hard-restart — caller reconnects instead.
        assert await mgr.restart("shared_backed") is False
        assert await mgr.restart("ext_backed") is False


@pytest.mark.anyio
async def test_facade_plain_probe_background(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plain url-only servers: probe runs in the background, boot not blocked."""
    import asyncio

    config = _make_mixed_config()
    config.servers["shared_backed"].shared_server = None
    config.servers["ext_backed"].external_server = None
    mgr = BackingProcessManager(config)
    _mock_both_polls(monkeypatch, ready=True)

    with patch("asyncio.create_subprocess_exec", _mock_async_process(returncode=0)):
        await mgr.start_all()
        # Give the background probes a tick.
        probe = mgr.probe
        assert probe is not None
        if probe._tasks:
            await asyncio.gather(*list(probe._tasks.values()), return_exceptions=True)

    assert mgr.backing_state("shared_backed") == "reachable"
    assert mgr.backing_state("ext_backed") == "reachable"
    assert mgr.backing_state("nonexistent") is None
