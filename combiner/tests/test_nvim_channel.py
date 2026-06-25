"""Integration test for the Neovim back-channel.

Starts a real headless ``nvim --listen <socket>`` with the plugin's Lua on the
runtimepath, then drives ``NvimChannelManager`` end to end: register → call the
single ``dispatch`` entry point → assert the MCP-shaped result round-trips.

Skipped automatically if ``nvim`` is not on PATH.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from mcp_combiner.nvim_channel import NoInstanceError, NvimChannelManager

# lua/ lives at <repo>/lua; this file is <repo>/combiner/tests/test_nvim_channel.py
_REPO_ROOT = Path(__file__).resolve().parents[2]
_LUA_DIR = _REPO_ROOT / "lua"

pytestmark = pytest.mark.skipif(shutil.which("nvim") is None, reason="nvim not installed")


async def _wait_for_socket(path: str, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if os.path.exists(path):
            return
        await asyncio.sleep(0.05)
    raise TimeoutError(f"nvim socket never appeared at {path}")


@pytest.fixture
async def nvim_instance() -> AsyncIterator[str]:
    """Launch a headless nvim with the plugin set up; yield its socket path."""
    tmpdir = tempfile.mkdtemp(prefix="mcpc-nvim-")
    socket = os.path.join(tmpdir, "nvim.sock")

    setup_lua = (
        "lua require('mcp_companion.native').setup("
        "{native_servers={neovim={enabled=true}}})"
    )
    seed_buf = "lua vim.api.nvim_buf_set_lines(0,0,-1,false,{'alpha','beta','gamma'})"

    proc = subprocess.Popen(
        [
            "nvim",
            "--headless",
            "--noplugin",
            "-u",
            "NONE",
            "--listen",
            socket,
            "-c",
            f"set rtp+={_LUA_DIR.parent}",
            "-c",
            setup_lua,
            "-c",
            seed_buf,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        await _wait_for_socket(socket)
        yield socket
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(tmpdir, ignore_errors=True)


async def test_dispatch_round_trip(nvim_instance: str) -> None:
    mgr = NvimChannelManager()
    mgr.register("inst-1", nvim_instance)
    try:
        # list_buffers → structured JSON text payload
        result = await mgr.call("inst-1", "list_buffers", {})
        assert isinstance(result, dict)
        assert "content" in result
        assert not result.get("isError")
        text = result["content"][0]["text"]
        assert "buffers" in text

        # read_buffer on the seeded buffer
        read = await mgr.call("inst-1", "read_buffer", {"buffer": 1})
        assert "alpha" in read["content"][0]["text"]
        assert "gamma" in read["content"][0]["text"]
    finally:
        mgr.deregister("inst-1")


async def test_unknown_tool_returns_is_error(nvim_instance: str) -> None:
    mgr = NvimChannelManager()
    mgr.register("inst-1", nvim_instance)
    try:
        result = await mgr.call("inst-1", "does_not_exist", {})
        assert result.get("isError") is True
    finally:
        mgr.deregister("inst-1")


async def test_edit_round_trip_preserves_order(nvim_instance: str) -> None:
    """Two serialized writes apply in submission order via the per-instance queue."""
    mgr = NvimChannelManager()
    mgr.register("inst-1", nvim_instance)
    try:
        # Fire two edits concurrently; the FIFO queue must apply them in order.
        a = mgr.call(
            "inst-1",
            "set_buffer_lines",
            {"buffer": 1, "start": 1, "end": 1, "lines": ["one"]},
        )
        b = mgr.call(
            "inst-1",
            "set_buffer_lines",
            {"buffer": 1, "start": 1, "end": 1, "lines": ["two"]},
        )
        await asyncio.gather(a, b)
        final = await mgr.call("inst-1", "read_buffer", {"buffer": 1})
        # Last write wins → line 1 is "two"
        assert "1\ttwo" in final["content"][0]["text"]
    finally:
        mgr.deregister("inst-1")


async def test_call_unregistered_instance_raises() -> None:
    mgr = NvimChannelManager()
    with pytest.raises(NoInstanceError):
        await mgr.call("ghost", "list_buffers", {})


async def test_manifest_captured_and_frozen(nvim_instance: str) -> None:
    mgr = NvimChannelManager()
    mgr.register("inst-1", nvim_instance)
    try:
        manifest = await mgr.ensure_manifest()
        assert manifest is not None
        assert "neovim" in manifest
        tools = manifest["neovim"]["tools"]
        names = {t["name"] for t in tools}
        # A representative sampling across tiers must be present.
        assert {"read_buffer", "edit_buffer", "get_diagnostics", "open_file"} <= names
        for t in tools:
            assert t["inputSchema"]["type"] == "object"

        # Frozen: a second call returns the identical captured object.
        assert await mgr.ensure_manifest() is manifest
        assert mgr.manifest() is manifest
    finally:
        mgr.deregister("inst-1")


async def test_manifest_none_until_an_instance_connects() -> None:
    mgr = NvimChannelManager()
    assert mgr.manifest() is None
    assert await mgr.ensure_manifest() is None
