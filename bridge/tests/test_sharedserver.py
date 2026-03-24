"""Tests for sharedserver integration."""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from mcp_bridge.config import BridgeConfig, SharedServerConfig
from mcp_bridge.sharedserver import SharedServerManager, _build_use_cmd, _require_binary


def _mock_async_process(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> AsyncMock:
    """Create a mock for asyncio.create_subprocess_exec that returns a process."""
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))

    factory = AsyncMock(return_value=proc)
    return factory


# ── _require_binary ────────────────────────────────────────────────


def test_require_binary_found() -> None:
    with patch("shutil.which", return_value="/usr/local/bin/sharedserver"):
        result = _require_binary()
    assert result == "/usr/local/bin/sharedserver"


def test_require_binary_not_found() -> None:
    with patch("shutil.which", return_value=None):
        with pytest.raises(FileNotFoundError, match="sharedserver not found on PATH"):
            _require_binary()


# ── _build_use_cmd ─────────────────────────────────────────────────


def _make_ss(**kwargs: Any) -> SharedServerConfig:
    defaults: dict[str, Any] = {
        "name": "goog_ws",
        "command": "uvx",
        "args": ["workspace-mcp", "--transport", "streamable-http"],
        "env": {"WORKSPACE_MCP_PORT": "8002", "MCP_ENABLE_OAUTH21": "true"},
    }
    defaults.update(kwargs)
    return SharedServerConfig(**defaults)


def test_build_use_cmd_basic() -> None:
    ss = _make_ss()
    cmd = _build_use_cmd("/usr/bin/sharedserver", ss, interpolate=False)
    assert cmd[0] == "/usr/bin/sharedserver"
    assert cmd[1] == "use"
    assert cmd[2] == "goog_ws"
    assert "--" in cmd
    sep = cmd.index("--")
    assert cmd[sep + 1] == "uvx"
    assert cmd[sep + 2 :] == ["workspace-mcp", "--transport", "streamable-http"]


def test_build_use_cmd_includes_env() -> None:
    ss = _make_ss()
    cmd = _build_use_cmd("/usr/bin/sharedserver", ss, interpolate=False)
    env_flags = [cmd[i + 1] for i, v in enumerate(cmd) if v == "--env"]
    assert "WORKSPACE_MCP_PORT=8002" in env_flags
    assert "MCP_ENABLE_OAUTH21=true" in env_flags


def test_build_use_cmd_grace_period() -> None:
    ss = _make_ss(grace_period="30m")
    cmd = _build_use_cmd("/usr/bin/sharedserver", ss, interpolate=False)
    assert "--grace-period" in cmd
    idx = cmd.index("--grace-period")
    assert cmd[idx + 1] == "30m"


def test_build_use_cmd_no_grace_period() -> None:
    ss = _make_ss(grace_period=None)
    cmd = _build_use_cmd("/usr/bin/sharedserver", ss, interpolate=False)
    assert "--grace-period" not in cmd


def test_build_use_cmd_includes_pid() -> None:
    import os

    ss = _make_ss()
    cmd = _build_use_cmd("/usr/bin/sharedserver", ss, interpolate=False)
    assert "--pid" in cmd
    idx = cmd.index("--pid")
    assert cmd[idx + 1] == str(os.getpid())


def test_build_use_cmd_explicit_pid() -> None:
    ss = _make_ss()
    cmd = _build_use_cmd("/usr/bin/sharedserver", ss, interpolate=False, pid=12345)
    assert "--pid" in cmd
    idx = cmd.index("--pid")
    assert cmd[idx + 1] == "12345"


def test_build_use_cmd_empty_env() -> None:
    ss = _make_ss(env={})
    cmd = _build_use_cmd("/usr/bin/sharedserver", ss, interpolate=False)
    assert "--env" not in cmd


# ── SharedServerManager ────────────────────────────────────────────


def _make_config(*, sharedserver_data: dict[str, Any] | None = None) -> BridgeConfig:
    ss = sharedserver_data or {
        "command": "uvx",
        "args": ["workspace-mcp", "--transport", "streamable-http"],
        "env": {"WORKSPACE_MCP_PORT": "8002"},
        "grace_period": "30m",
        "health_timeout": 1,
    }
    raw = {
        "sharedServers": {"goog_ws": ss},
        "servers": {
            "google-workspace": {
                "url": "http://localhost:8002/mcp",
                "auth": "oauth",
                "sharedServer": "goog_ws",
            }
        },
    }

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(raw, f)
        path = f.name
    config = BridgeConfig.load(path)
    os.unlink(path)
    return config


@pytest.mark.anyio
async def test_start_all_calls_sharedserver_use() -> None:
    config = _make_config()
    mgr = SharedServerManager(config)

    mock_exec = _mock_async_process(returncode=0)
    with (
        patch("shutil.which", return_value="/usr/bin/sharedserver"),
        patch("asyncio.create_subprocess_exec", mock_exec),
        patch("mcp_bridge.sharedserver._poll_url", new=AsyncMock(return_value=True)),
    ):
        await mgr.start_all()

    assert mock_exec.called
    # The first positional args are the command parts
    cmd = list(mock_exec.call_args[0])
    assert cmd[0] == "/usr/bin/sharedserver"
    assert cmd[1] == "use"
    assert cmd[2] == "goog_ws"
    assert "goog_ws" in mgr._active


@pytest.mark.anyio
async def test_start_all_skips_when_binary_missing() -> None:
    config = _make_config()
    mgr = SharedServerManager(config)

    with patch("shutil.which", return_value=None):
        await mgr.start_all()

    assert mgr._active == []


@pytest.mark.anyio
async def test_start_all_skips_on_nonzero_exit() -> None:
    config = _make_config()
    mgr = SharedServerManager(config)

    mock_exec = _mock_async_process(returncode=1, stderr=b"error")
    with (
        patch("shutil.which", return_value="/usr/bin/sharedserver"),
        patch("asyncio.create_subprocess_exec", mock_exec),
    ):
        await mgr.start_all()

    assert mgr._active == []


@pytest.mark.anyio
async def test_stop_all_calls_unuse() -> None:
    config = _make_config()
    mgr = SharedServerManager(config)
    mgr._binary = "/usr/bin/sharedserver"
    mgr._active = ["goog_ws"]

    mock_exec = _mock_async_process(returncode=0)
    with patch("asyncio.create_subprocess_exec", mock_exec):
        await mgr.stop_all()

    mock_exec.assert_called_once()
    cmd = list(mock_exec.call_args[0])
    assert cmd == ["/usr/bin/sharedserver", "unuse", "goog_ws"]
    assert mgr._active == []


@pytest.mark.anyio
async def test_stop_all_noop_when_nothing_started() -> None:
    config = _make_config()
    mgr = SharedServerManager(config)

    mock_exec = _mock_async_process()
    with patch("asyncio.create_subprocess_exec", mock_exec):
        await mgr.stop_all()

    mock_exec.assert_not_called()


@pytest.mark.anyio
async def test_start_all_no_sharedserver_config() -> None:
    """Servers without sharedserver config are silently skipped."""
    raw = {"servers": {"plain": {"url": "http://localhost:9000/mcp"}}}

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(raw, f)
        path = f.name
    config = BridgeConfig.load(path)
    os.unlink(path)

    mgr = SharedServerManager(config)
    mock_exec = _mock_async_process()
    with patch("asyncio.create_subprocess_exec", mock_exec):
        await mgr.start_all()

    mock_exec.assert_not_called()
    assert mgr._active == []


@pytest.mark.anyio
async def test_health_poll_timeout_warns_but_continues() -> None:
    """A server that doesn't become healthy still gets added to _active."""
    config = _make_config()
    mgr = SharedServerManager(config)

    mock_exec = _mock_async_process(returncode=0)
    with (
        patch("shutil.which", return_value="/usr/bin/sharedserver"),
        patch("asyncio.create_subprocess_exec", mock_exec),
        patch("mcp_bridge.sharedserver._poll_url", new=AsyncMock(return_value=False)),
    ):
        await mgr.start_all()

    # Should still be tracked so unuse is called on exit
    assert "goog_ws" in mgr._active
