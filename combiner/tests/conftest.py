"""Shared fixtures for combiner tests.

Process-level fixtures for the e2e matrix: spawn the combiner and mock
upstreams (mcp_combiner.mockserver) as real subprocesses, generate servers.json
configs, and poll health. Tests that spawn processes are marked `e2e`.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

COMBINER_DIR = Path(__file__).resolve().parents[1]

SHAREDSERVER_BIN = shutil.which("sharedserver")

requires_sharedserver = pytest.mark.skipif(
    SHAREDSERVER_BIN is None, reason="sharedserver binary not installed"
)

# Timing knobs (connections.py env overrides) so reconnect/escalation paths run
# in seconds instead of minutes under test. Defaults in production are unchanged.
FAST_TIMING_ENV = {
    "MCP_COMBINER_HEALTH_INTERVAL": "1",
    "MCP_COMBINER_INITIAL_BACKOFF": "0.5",
    "MCP_COMBINER_MAX_BACKOFF": "2",
    "MCP_COMBINER_MAX_BACKOFF_LOCAL": "1",
}


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def mockserver_argv(
    name: str,
    transport: str,
    port: int | None = None,
    tools_path: Path | None = None,
    state_dir: Path | None = None,
) -> list[str]:
    """Argv for `python -m mcp_combiner.mockserver` — usable directly for a
    subprocess (http mode) or as a stdio config entry's command/args."""
    argv = [sys.executable, "-m", "mcp_combiner.mockserver"]
    argv += ["--name", name, "--transport", transport]
    if port is not None:
        argv += ["--port", str(port)]
    if tools_path is not None:
        argv += ["--tools", str(tools_path)]
    if state_dir is not None:
        argv += ["--state-dir", str(state_dir)]
    return argv


def stdio_mock_entry(
    name: str,
    tools_path: Path | None = None,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    """servers.json entry for a stdio-transport mock upstream."""
    argv = mockserver_argv(name, "stdio", tools_path=tools_path, state_dir=state_dir)
    return {"command": argv[0], "args": argv[1:], "env": {}}


def http_mock_entry(port: int) -> dict[str, Any]:
    """servers.json entry for a raw-HTTP mock upstream."""
    return {"url": f"http://127.0.0.1:{port}/mcp", "transport": "http"}


def sharedserver_mock_entry(
    name: str,
    shared_name: str,
    port: int,
) -> dict[str, Any]:
    """servers.json entry for a sharedserver-backed mock upstream."""
    return {
        "url": f"http://127.0.0.1:{port}/mcp",
        "transport": "http",
        "sharedServer": shared_name,
    }


def sharedserver_def(
    name: str,
    port: int,
    tools_path: Path | None = None,
    state_dir: Path | None = None,
    grace_period: str = "2s",
) -> dict[str, Any]:
    """Top-level sharedServers definition launching the mock over HTTP."""
    argv = mockserver_argv(name, "http", port=port, tools_path=tools_path, state_dir=state_dir)
    return {
        "command": argv[0],
        "args": argv[1:],
        "env": {},
        "grace_period": grace_period,
        "health_timeout": 30,
    }


def write_servers_config(
    path: Path,
    servers: dict[str, Any],
    shared_servers: dict[str, Any] | None = None,
) -> Path:
    doc: dict[str, Any] = {"servers": servers}
    if shared_servers:
        doc["sharedServers"] = shared_servers
    path.write_text(json.dumps(doc, indent=2))
    return path


def write_tools_spec(path: Path, spec: list[dict[str, Any]]) -> Path:
    path.write_text(json.dumps(spec))
    return path


async def poll_until(
    fn: Callable[[], Awaitable[Any]],
    timeout: float = 30.0,
    interval: float = 0.25,
    desc: str = "condition",
) -> Any:
    """Poll an async predicate until it returns a truthy value."""
    deadline = asyncio.get_running_loop().time() + timeout
    last_exc: Exception | None = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            result = await fn()
            if result:
                return result
        except Exception as exc:  # noqa: BLE001 - polling swallows by design
            last_exc = exc
        await asyncio.sleep(interval)
    suffix = f" (last error: {last_exc})" if last_exc else ""
    raise TimeoutError(f"timed out waiting for {desc}{suffix}")


@dataclass
class CombinerHandle:
    """A running combiner subprocess and how to reach it."""

    proc: subprocess.Popen[bytes]
    port: int
    config_path: Path

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def mcp_url(self) -> str:
        return f"{self.base_url}/mcp"

    async def health(self) -> dict[str, Any]:
        async with httpx.AsyncClient() as http:
            r = await http.get(f"{self.base_url}/health", timeout=2.0)
            r.raise_for_status()
            return dict(r.json())

    async def wait_healthy(self, timeout: float = 30.0) -> dict[str, Any]:
        async def _ok() -> dict[str, Any] | None:
            h = await self.health()
            return h if h.get("status") == "ok" else None

        result: dict[str, Any] = await poll_until(
            _ok, timeout=timeout, desc=f"combiner health on :{self.port}"
        )
        return result

    async def wait_server_state(
        self, name: str, states: tuple[str, ...] = ("ready",), timeout: float = 30.0
    ) -> dict[str, Any]:
        async def _state() -> dict[str, Any] | None:
            h = await self.health()
            info = h.get("servers", {}).get(name)
            return info if info and info.get("state") in states else None

        result: dict[str, Any] = await poll_until(
            _state, timeout=timeout, desc=f"server {name} state in {states}"
        )
        return result

    def terminate(self, timeout: float = 10.0) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)


@dataclass
class MockHandle:
    """A running HTTP mock upstream subprocess."""

    proc: subprocess.Popen[bytes]
    port: int
    name: str

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def mcp_url(self) -> str:
        return f"{self.base_url}/mcp"

    async def stats(self) -> dict[str, Any]:
        async with httpx.AsyncClient() as http:
            r = await http.get(f"{self.base_url}/stats", timeout=2.0)
            r.raise_for_status()
            return dict(r.json())

    async def control(self, method: str, path: str, body: Any = None) -> Any:
        async with httpx.AsyncClient() as http:
            r = await http.request(method, f"{self.base_url}{path}", json=body, timeout=5.0)
            r.raise_for_status()
            return r.json()

    async def wait_healthy(self, timeout: float = 30.0) -> None:
        async def _ok() -> bool:
            async with httpx.AsyncClient() as http:
                r = await http.get(f"{self.base_url}/health", timeout=1.0)
                return r.status_code == 200

        await poll_until(_ok, timeout=timeout, desc=f"mock {self.name} health on :{self.port}")

    def terminate(self, timeout: float = 10.0) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)


def unique_shared_name(prefix: str = "mockshared") -> str:
    """Unique sharedserver process name per test.

    The sharedserver daemon is machine-global and keyed by name: reusing a
    name across tests attaches to a previous test's still-alive process
    (grace period) instead of launching this test's config.
    """
    import uuid

    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@dataclass
class ProcFactory:
    """Tracks spawned subprocesses for teardown."""

    combiners: list[CombinerHandle] = field(default_factory=list)
    mocks: list[MockHandle] = field(default_factory=list)
    shared_names: list[str] = field(default_factory=list)

    def track_shared(self, name: str) -> str:
        """Register a sharedserver name for force-stop at teardown (orphan guard)."""
        self.shared_names.append(name)
        return name

    async def start_combiner(
        self,
        config_path: Path,
        port: int | None = None,
        extra_args: list[str] | None = None,
        env: dict[str, str] | None = None,
        wait: bool = True,
    ) -> CombinerHandle:
        port = port or free_port()
        argv = [
            sys.executable,
            "-m",
            "mcp_combiner",
            "--mcp",
            "--config",
            str(config_path),
            "--port",
            str(port),
        ] + (extra_args or [])
        proc = subprocess.Popen(
            argv,
            cwd=str(COMBINER_DIR),
            env={**os.environ, **env} if env else None,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        handle = CombinerHandle(proc=proc, port=port, config_path=config_path)
        self.combiners.append(handle)
        if wait:
            await handle.wait_healthy()
        return handle

    async def start_http_mock(
        self,
        name: str,
        port: int | None = None,
        tools_path: Path | None = None,
        state_dir: Path | None = None,
        wait: bool = True,
    ) -> MockHandle:
        port = port or free_port()
        proc = subprocess.Popen(
            mockserver_argv(name, "http", port=port, tools_path=tools_path, state_dir=state_dir),
            cwd=str(COMBINER_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        handle = MockHandle(proc=proc, port=port, name=name)
        self.mocks.append(handle)
        if wait:
            await handle.wait_healthy()
        return handle

    def teardown(self) -> None:
        for m in self.mocks:
            m.terminate()
        for c in self.combiners:
            c.terminate()
        # Force-stop sharedserver-managed processes so no orphan squats its
        # port/name for the next test (the daemon's grace period would keep
        # them alive otherwise).
        for name in self.shared_names:
            if SHAREDSERVER_BIN is not None:
                subprocess.run(
                    [SHAREDSERVER_BIN, "admin", "stop", "--force", name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                    check=False,
                )


@pytest.fixture
async def procs() -> AsyncIterator[ProcFactory]:
    factory = ProcFactory()
    yield factory
    factory.teardown()
