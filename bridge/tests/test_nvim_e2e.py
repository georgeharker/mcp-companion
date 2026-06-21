"""End-to-end test of both Phase 2 halves.

Starts a real bridge subprocess and a real headless Neovim with the plugin.
The plugin's `channel.lua` opens its own socket and registers/binds itself with
the bridge over REST. Then an MCP client connects to `/mcp/<token>` and calls a
`neovim_*` tool, which the bridge routes back into the live editor.

Exercises: channel.lua → /neovim/instances + /neovim/bind → NvimChannelManager
→ frozen manifest over the channel → ToolProcessingMiddleware injection +
interception → dispatch in Neovim → result back to the MCP client.

Skipped if `nvim` is not installed.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastmcp import Client

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN_ROOT = _REPO_ROOT  # rtp root (contains lua/)
_BRIDGE_DIR = _REPO_ROOT / "bridge"
_PORT = 9743
_TOKEN = "abcdef01-2345-6789-abcd-ef0123456789"

pytestmark = pytest.mark.skipif(shutil.which("nvim") is None, reason="nvim not installed")


async def _poll_health(timeout: float = 20.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    async with httpx.AsyncClient() as http:
        while asyncio.get_running_loop().time() < deadline:
            try:
                r = await http.get(f"http://127.0.0.1:{_PORT}/health", timeout=1.0)
                if r.status_code == 200 and r.json().get("status") == "ok":
                    return
            except Exception:
                pass
            await asyncio.sleep(0.25)
    raise TimeoutError("bridge did not become healthy")


@pytest.fixture
async def bridge_and_nvim() -> AsyncIterator[None]:
    tmpdir = tempfile.mkdtemp(prefix="mcpc-e2e-")
    cfg_path = os.path.join(tmpdir, "servers.json")
    with open(cfg_path, "w") as f:
        json.dump({"mcpServers": {}}, f)  # no upstreams — keeps startup fast

    bridge = subprocess.Popen(
        [sys.executable, "-m", "mcp_bridge", "--config", cfg_path, "--port", str(_PORT)],
        cwd=str(_BRIDGE_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Headless nvim: set up the plugin, seed a buffer, then register + bind over
    # the channel. channel.start() opens its own socket and POSTs to the bridge.
    setup = (
        f"lua require('mcp_companion.config').setup({{bridge={{port={_PORT},host='127.0.0.1'}}}})"
    )
    native = "lua require('mcp_companion.native').setup({native_servers={neovim={enabled=true}}})"
    seed = "lua vim.api.nvim_buf_set_lines(0,0,-1,false,{'alpha','beta','gamma'})"
    start = "lua require('mcp_companion.native.channel').start()"
    bind = f"lua require('mcp_companion.native.channel').bind('{_TOKEN}')"

    nvim = subprocess.Popen(
        [
            "nvim", "--headless", "--noplugin", "-u", "NONE",
            "-c", f"set rtp+={_PLUGIN_ROOT}",
            "-c", setup, "-c", native, "-c", seed, "-c", start, "-c", bind,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        await _poll_health()
        yield None
    finally:
        nvim.terminate()
        bridge.terminate()
        for p in (nvim, bridge):
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        shutil.rmtree(tmpdir, ignore_errors=True)


async def test_tokenless_client_must_name_instance() -> None:
    """A directly-configured agent (no token binding) sees neovim_* tools and can
    enumerate instances, but — having no editor association — must pass
    nvim_instance explicitly. Mirrors Claude Code via its own static MCP config.
    """
    import tempfile as _tf

    port = 9745
    token = "11111111-2222-3333-4444-555555555555"  # never bound on purpose
    tmpdir = _tf.mkdtemp(prefix="mcpc-tokenless-")
    cfg = os.path.join(tmpdir, "servers.json")
    with open(cfg, "w") as f:
        json.dump({"mcpServers": {}}, f)
    sock = os.path.join(tmpdir, "nv.sock")

    bridge = subprocess.Popen(
        [sys.executable, "-m", "mcp_bridge", "--config", cfg, "--port", str(port)],
        cwd=str(_BRIDGE_DIR), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    nvim = subprocess.Popen(
        ["nvim", "--headless", "--noplugin", "-u", "NONE", "--listen", sock,
         "-c", f"set rtp+={_PLUGIN_ROOT}",
         "-c", "lua require('mcp_companion.native')"
               + ".setup({native_servers={neovim={enabled=true}}})",
         "-c", "lua vim.api.nvim_buf_set_lines(0,0,-1,false,{'alpha','beta'})"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        async with httpx.AsyncClient() as http:
            deadline = asyncio.get_running_loop().time() + 20
            while asyncio.get_running_loop().time() < deadline:
                try:
                    r = await http.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
                    if r.status_code == 200:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.25)
            await asyncio.sleep(0.5)
            # Register the instance but do NOT bind any token.
            await http.post(
                f"http://127.0.0.1:{port}/neovim/instances",
                json={"instance_id": "solo", "socket": sock},
            )

        async with Client(f"http://127.0.0.1:{port}/mcp/{token}") as client:
            names: set[str] = set()
            for _ in range(20):
                names = {t.name for t in await client.list_tools()}
                if "neovim_read_buffer" in names:
                    break
                await asyncio.sleep(0.25)
            # Tools are advertised even to an unassociated connection.
            assert "neovim_read_buffer" in names, f"tokenless client saw {sorted(names)}"
            assert "neovim_list_instances" in names

            # Discovery works without targeting.
            listed = await client.call_tool("neovim_list_instances", {})
            ltext = "".join(
                b.text for b in listed.content if getattr(b, "type", None) == "text"
            )
            assert "solo" in ltext

            # No association + no nvim_instance → instructive error, not a guess.
            with pytest.raises(Exception) as ei:
                await client.call_tool("neovim_read_buffer", {"buffer": 1})
            assert "nvim_instance" in str(ei.value)

            # Explicit targeting routes to the chosen editor.
            result = await client.call_tool(
                "neovim_read_buffer", {"buffer": 1, "nvim_instance": "solo"}
            )
            text = "".join(
                b.text for b in result.content if getattr(b, "type", None) == "text"
            )
            assert "alpha" in text
    finally:
        nvim.terminate()
        bridge.terminate()
        for p in (nvim, bridge):
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        shutil.rmtree(tmpdir, ignore_errors=True)


async def test_agent_calls_neovim_tool_through_bridge(bridge_and_nvim: None) -> None:
    url = f"http://127.0.0.1:{_PORT}/mcp/{_TOKEN}"

    async with Client(url) as client:
        # Channel registration is async on the Neovim side; poll until the
        # virtual neovim tools surface for this (now token-bound) session.
        names: set[str] = set()
        for _ in range(40):
            tools = await client.list_tools()
            names = {t.name for t in tools}
            if "neovim_list_buffers" in names:
                break
            await asyncio.sleep(0.25)

        assert "neovim_list_buffers" in names, f"neovim tools not surfaced; saw {sorted(names)}"
        assert "neovim_read_buffer" in names

        # Call back into the live editor through the bridge.
        result = await client.call_tool("neovim_read_buffer", {"buffer": 1})
        text = "".join(
            block.text for block in result.content if getattr(block, "type", None) == "text"
        )
        assert "alpha" in text and "gamma" in text
