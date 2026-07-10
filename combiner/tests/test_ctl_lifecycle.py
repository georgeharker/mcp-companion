"""Tests for the start/stop/restart process-lifecycle CLI verbs (ctl.py)."""

from __future__ import annotations

import argparse
import os
from unittest.mock import AsyncMock, patch

import pytest

from mcp_combiner import ctl

# ── pure argv builders ─────────────────────────────────────────────


def test_build_start_cmd_attaches_pid_and_serve_argv() -> None:
    serve = ["/bin/mcp-combiner", "--mcp", "--config", "/c.json", "--port", "9741"]
    cmd = ctl._build_start_cmd(
        "/bin/sharedserver", name="mcp-combiner", pid=4242, grace_period="30m", serve_argv=serve
    )
    assert cmd[:3] == ["/bin/sharedserver", "use", "mcp-combiner"]
    # PID is passed explicitly (never defaulted to the caller/CLI).
    assert cmd[cmd.index("--pid") + 1] == "4242"
    assert cmd[cmd.index("--grace-period") + 1] == "30m"
    assert cmd[cmd.index("--metadata") + 1] == "cli-4242"
    sep = cmd.index("--")
    assert cmd[sep + 1 :] == serve


def test_build_start_cmd_includes_log_file_when_set() -> None:
    cmd = ctl._build_start_cmd(
        "ss", name="n", pid=1, grace_period="5m", serve_argv=["x"], log_file="/l.log"
    )
    assert cmd[cmd.index("--log-file") + 1] == "/l.log"


def test_build_start_cmd_omits_log_file_when_unset() -> None:
    cmd = ctl._build_start_cmd("ss", name="n", pid=1, grace_period="5m", serve_argv=["x"])
    assert "--log-file" not in cmd


def test_build_stop_cmd() -> None:
    assert ctl._build_stop_cmd("/bin/sharedserver", name="mcp-combiner", pid=99) == [
        "/bin/sharedserver",
        "unuse",
        "mcp-combiner",
        "--pid",
        "99",
    ]


def test_combiner_serve_argv_prefers_installed_entrypoint() -> None:
    with patch("shutil.which", return_value="/opt/bin/mcp-combiner"):
        argv = ctl._combiner_serve_argv("/c.json", "127.0.0.1", 9741, [])
    assert argv[0] == "/opt/bin/mcp-combiner"
    assert argv[1:] == ["--mcp", "--config", "/c.json", "--port", "9741", "--host", "127.0.0.1"]


def test_combiner_serve_argv_falls_back_to_module() -> None:
    with patch("shutil.which", return_value=None):
        argv = ctl._combiner_serve_argv("/c.json", "127.0.0.1", 9741, ["--no-output-validation"])
    assert argv[1:3] == ["-m", "mcp_combiner"]
    assert argv[-1] == "--no-output-validation"


# ── config resolution ──────────────────────────────────────────────


def test_resolve_config_explicit_wins() -> None:
    assert ctl._resolve_config("/explicit.json") == "/explicit.json"


def test_resolve_config_uses_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_COMBINER_CONFIG", "/from-env.json")
    assert ctl._resolve_config(None) == "/from-env.json"


def test_resolve_config_none_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCP_COMBINER_CONFIG", raising=False)
    monkeypatch.delenv("CLAUDE_MCP_COMBINER_CONFIG", raising=False)
    with patch("os.path.isfile", return_value=False):
        assert ctl._resolve_config(None) is None


# ── restart refcount guard (like :MCPRestart!) ─────────────────────


def _restart_args(**kw: object) -> argparse.Namespace:
    defaults: dict[str, object] = {
        "force": False,
        "config": "/c.json",
        "host": "127.0.0.1",
        "port": 9741,
        "name": "mcp-combiner",
        "grace_period": "30m",
        "pid": 4242,
        "log_file": None,
        "wait": False,
        "dry_run": True,
    }
    defaults.update(kw)
    return argparse.Namespace(**defaults)


@pytest.mark.asyncio
async def test_restart_refuses_when_other_clients_and_no_force(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch("mcp_combiner.sharedserver._require_binary", return_value="/bin/sharedserver"),
        patch.object(ctl, "_sharedserver_refcount", AsyncMock(return_value=3)),
    ):
        rc = await ctl.cmd_restart_combiner(_restart_args(force=False))
    assert rc == 1
    assert "clients attached" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_restart_proceeds_with_force(capsys: pytest.CaptureFixture[str]) -> None:
    # dry_run=True short-circuits before any real subprocess; force bypasses the guard.
    with (
        patch("mcp_combiner.sharedserver._require_binary", return_value="/bin/sharedserver"),
        patch.object(ctl, "_sharedserver_refcount", AsyncMock(return_value=3)),
        patch("os.path.isfile", return_value=True),
    ):
        rc = await ctl.cmd_restart_combiner(_restart_args(force=True))
    assert rc == 0
    out = capsys.readouterr().out
    # Prints the two sharedserver commands it *would* run: stop --force, then use.
    assert "admin stop --force" in out
    assert " use " in out


@pytest.mark.asyncio
async def test_restart_sole_client_needs_no_force(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("mcp_combiner.sharedserver._require_binary", return_value="/bin/sharedserver"),
        patch.object(ctl, "_sharedserver_refcount", AsyncMock(return_value=1)),
        patch("os.path.isfile", return_value=True),
    ):
        rc = await ctl.cmd_restart_combiner(_restart_args(force=False))
    assert rc == 0


@pytest.mark.asyncio
async def test_restart_combiner_pid_defaults_to_parent_shell(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # When --pid is omitted the reference attaches to the calling shell
    # (os.getppid()), never to this short-lived CLI process (os.getpid()).
    with (
        patch("mcp_combiner.sharedserver._require_binary", return_value="/bin/sharedserver"),
        patch.object(ctl, "_sharedserver_refcount", AsyncMock(return_value=1)),
        patch("os.path.isfile", return_value=True),
    ):
        rc = await ctl.cmd_restart_combiner(_restart_args(pid=None))
    assert rc == 0
    use_line = next(line for line in capsys.readouterr().out.splitlines() if " use " in line)
    tokens = use_line.split()
    assert tokens[tokens.index("--pid") + 1] == str(os.getppid())
